use std::path::PathBuf;
use std::time::Instant;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};

use fsim::model;
use fsim::render::{upscale, write_png, GifWriter, LampGrid};
use fsim::sim::Sim;

#[derive(Parser)]
#[command(about = "Tick-accurate Factorio 2.1 circuit simulator")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Simulate a blueprint (string or JSON file) and capture lamps / probes.
    Run {
        blueprint: PathBuf,
        /// Ticks to simulate (default: the case's fsim.ticks, else 60).
        #[arg(long)]
        ticks: Option<usize>,
        /// Game tick of simulation tick 0 (what time-mode selectors output).
        #[arg(long, default_value_t = 0)]
        start_tick: i64,
        /// Write lamp frames as PNGs into this directory.
        #[arg(long)]
        frames: Option<PathBuf>,
        /// Write lamp frames as raw PPM files (one pixel per lamp tile, for scripts) into this directory.
        #[arg(long)]
        ppm: Option<PathBuf>,
        /// Write lamp frames as an animated GIF.
        #[arg(long)]
        gif: Option<PathBuf>,
        /// Capture a frame every N ticks.
        #[arg(long, default_value_t = 1)]
        every: usize,
        /// Pixel size of one lamp in images.
        #[arg(long, default_value_t = 8)]
        scale: usize,
        /// Print an entity's output every tick (repeatable, by entity_number).
        #[arg(long = "probe")]
        probes: Vec<u32>,
        /// Print a network every tick: ENTITY:CONNECTOR (1/2 = red/green in, 3/4 = red/green out).
        #[arg(long = "net")]
        nets: Vec<String>,
    },
    /// Compare fsim with a game dump from harness/run.py.
    Conform {
        #[arg(default_value = "conformance/cases")]
        cases: PathBuf,
        #[arg(long, default_value = "conformance/game_dump.json")]
        dump: PathBuf,
        /// Only this case.
        #[arg(long)]
        case: Option<String>,
    },
    /// Print a blueprint string as JSON.
    Decode { blueprint: PathBuf },
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Decode { blueprint } => {
            println!("{}", serde_json::to_string_pretty(&model::load_file(&blueprint)?)?);
        }
        Cmd::Conform { cases, dump, case } => {
            let results = fsim::conform::run(&cases, &dump, case.as_deref())?;
            let mut failed = 0;
            for r in &results {
                if r.mismatches.is_empty() {
                    println!("PASS {:<20} {} checks", r.name, r.checks);
                } else {
                    failed += 1;
                    println!("FAIL {:<20} {} of {} checks differ", r.name, r.mismatches.len(), r.checks);
                    for m in r.mismatches.iter().take(12) {
                        println!("    {m}");
                    }
                    if r.mismatches.len() > 12 {
                        println!("    ... {} more", r.mismatches.len() - 12);
                    }
                }
            }
            println!("{} of {} cases match the game", results.len() - failed, results.len());
            if failed > 0 {
                std::process::exit(1);
            }
        }
        Cmd::Run { blueprint, ticks, start_tick, frames, ppm, gif, every, scale, probes, nets } => {
            let m = model::parse(&model::load_file(&blueprint)?)?;
            for w in &m.warnings {
                eprintln!("warning: {w}");
            }
            let ticks = ticks.or_else(|| m.meta.get("ticks").and_then(|t| t.as_u64()).map(|t| t as usize)).unwrap_or(60);
            let mut sim = Sim::new(m, start_tick);
            let mut net_probes = Vec::new();
            for spec in &nets {
                let Some((e, c)) = spec.split_once(':') else { bail!("--net wants ENTITY:CONNECTOR") };
                let (num, c): (u32, usize) = (e.parse()?, c.parse()?);
                let Some(idx) = sim.model.entity_index(num) else { bail!("no entity {num}") };
                net_probes.push((spec.clone(), sim.conn[idx][c]));
            }
            let probe_idx: Vec<(u32, usize)> =
                probes.iter().filter_map(|&n| sim.model.entity_index(n).map(|i| (n, i))).collect();
            let grid = LampGrid::new(&sim);
            if (frames.is_some() || gif.is_some() || ppm.is_some()) && grid.is_none() {
                bail!("blueprint has no lamps to render");
            }
            for dir in frames.iter().chain(ppm.iter()) {
                std::fs::create_dir_all(dir)?;
            }
            let mut gif_out = match (&gif, &grid) {
                (Some(p), Some(g)) => {
                    let delay = ((every as f64 / 60.0) * 100.0).round().max(2.0) as u16;
                    Some(GifWriter::create(p, g.width * scale, g.height * scale, delay)?)
                }
                _ => None,
            };
            let combinators = sim.model.entities.iter().filter(|e| e.behavior.is_combinator()).count();
            eprintln!(
                "{}: {} entities, {} combinators, {} lamps, {} networks",
                sim.model.label,
                sim.model.entities.len(),
                combinators,
                grid.as_ref().map(|g| g.lamp_count()).unwrap_or(0),
                sim.net_count
            );
            let start = Instant::now();
            let mut sim_time = 0.0;
            for t in 0..ticks {
                let s = Instant::now();
                sim.step();
                sim_time += s.elapsed().as_secs_f64();
                for &(num, i) in &probe_idx {
                    println!("t={t} #{num} {}: {}", sim.model.entities[i].name, sim.model.signals.format(&sim.outputs[i]));
                }
                for (spec, net) in &net_probes {
                    let v = net.map(|n| sim.model.signals.format(&sim.nets[n])).unwrap_or_else(|| "(unconnected)".into());
                    println!("t={t} net {spec}: {v}");
                }
                if t % every == 0 {
                    if let Some(g) = &grid {
                        if let Some(dir) = &ppm {
                            let mut data = format!("P6\n{} {}\n255\n", g.width, g.height).into_bytes();
                            data.extend_from_slice(&g.frame(&sim));
                            std::fs::write(dir.join(format!("{t:05}.ppm")), data)?;
                        }
                        if frames.is_some() || gif_out.is_some() {
                            let img = upscale(&g.frame(&sim), g.width, g.height, scale);
                            if let Some(dir) = &frames {
                                write_png(&dir.join(format!("{t:05}.png")), &img, g.width * scale, g.height * scale)?;
                            }
                            if let Some(gw) = &mut gif_out {
                                gw.push(&img)?;
                            }
                        }
                    }
                }
            }
            eprintln!(
                "simulated {ticks} ticks in {:.3}s ({:.1} µs/tick, {:.3}s total)",
                sim_time,
                sim_time * 1e6 / ticks.max(1) as f64,
                start.elapsed().as_secs_f64()
            );
        }
    }
    Ok(())
}
