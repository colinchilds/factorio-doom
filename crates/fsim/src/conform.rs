//! Compare fsim against what the real game computed (`harness/run.py` dump).
//!
//! Dump row k is read in the game's on_tick at game tick t0 + k, with every constant combinator
//! switched on at t0. In simulation terms (see sim.rs) that row shows:
//!   combinator output  = O(k - 2)
//!   lamp state         = lamp(k - 1)
//! and simulation tick 0 runs at game tick t0 + 1. Before t0 the game ran a warm-up with the
//! constant combinators off; fsim replays that too, so rows 0 and 1 show warm-up outputs.

use std::path::Path;

use anyhow::{anyhow, Context, Result};
use serde_json::Value;

use crate::model::{self, Behavior};
use crate::signals::{SigVec, SignalKey};
use crate::sim::{LampState, Sim};

pub struct CaseResult {
    pub name: String,
    pub checks: usize,
    pub mismatches: Vec<String>,
}

pub fn run(cases_dir: &Path, dump_path: &Path, only: Option<&str>) -> Result<Vec<CaseResult>> {
    let dump: Value = serde_json::from_str(&std::fs::read_to_string(dump_path)?)?;
    let t0 = dump.get("t0").and_then(Value::as_i64).ok_or_else(|| anyhow!("dump has no t0"))?;
    let cases = dump.get("cases").ok_or_else(|| anyhow!("dump has no cases"))?;
    let mut files: Vec<_> = std::fs::read_dir(cases_dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    let mut results = Vec::new();
    for f in files {
        let name = f.file_stem().unwrap().to_string_lossy().to_string();
        if only.is_some_and(|o| o != name) {
            continue;
        }
        let Some(rows) = cases.get(&name).and_then(Value::as_array) else {
            results.push(CaseResult { name, checks: 0, mismatches: vec!["not in game dump".into()] });
            continue;
        };
        let model = model::parse(&model::load_file(&f)?).with_context(|| f.display().to_string())?;
        let mut r = check_case(name, model, rows, t0)?;
        if let Some(list) = dump.pointer(&format!("/unpowered/{}", r.name)).and_then(Value::as_array) {
            if !list.is_empty() {
                r.mismatches.insert(0, format!("harness problem: entities {list:?} had no power in game"));
            }
        }
        results.push(r);
    }
    Ok(results)
}

fn check_case(name: String, model: model::Model, rows: &[Value], t0: i64) -> Result<CaseResult> {
    let warmup = (t0 - 2).clamp(0, 60);
    let mut sim = Sim::new(model, t0 + 1 - warmup);
    sim.set_constants(false);
    let mut pre_out = Vec::new();
    let mut pre_lamps = Vec::new();
    for _ in 0..warmup {
        sim.step();
        pre_out.push(sim.outputs.clone());
        pre_lamps.push(sim.lamps.clone());
    }
    sim.set_constants(true);
    let n = rows.len();
    let mut outs: Vec<Vec<SigVec>> = Vec::with_capacity(n);
    let mut lamps: Vec<Vec<Option<LampState>>> = Vec::with_capacity(n);
    for _ in 0..n {
        sim.step();
        outs.push(sim.outputs.clone());
        lamps.push(sim.lamps.clone());
    }
    let mut res = CaseResult { name, checks: 0, mismatches: Vec::new() };
    let mut order_reported = false;
    for (k, row) in rows.iter().enumerate() {
        let Some(row) = row.as_object() else { continue };
        for (num, entry) in row {
            let num: u32 = num.parse()?;
            let e = sim.model.entity_index(num).ok_or_else(|| anyhow!("dump entity {num} not in blueprint"))?;
            let ename = sim.model.entities[e].name.clone();
            if let Some(list) = entry.get("out") {
                let mut ids = Vec::new();
                for s in list.as_array().into_iter().flatten() {
                    let a = s.as_array().ok_or_else(|| anyhow!("bad signal entry"))?;
                    let key = SignalKey::new(
                        a[0].as_str().unwrap_or("item"),
                        a[1].as_str().unwrap_or(""),
                        a[2].as_str().unwrap_or("normal"),
                    );
                    let id = sim.model.signals.intern(key);
                    ids.push((id, a[3].as_i64().unwrap_or(0) as i32));
                }
                if !order_reported && ids.windows(2).any(|w| w[0].0 >= w[1].0) {
                    res.mismatches.push(format!("game listed signals in an order fsim doesn't share: {:?}", ids));
                    order_reported = true;
                }
                let mut expected = ids;
                expected.sort_unstable_by_key(|x| x.0);
                let actual = match (k as i64) - 2 {
                    t if t >= 0 => outs[t as usize][e].clone(),
                    t => pre_out.get((warmup + t) as usize).map(|o| o[e].clone()).unwrap_or_default(),
                };
                res.checks += 1;
                if expected != actual {
                    res.mismatches.push(format!(
                        "row {k} #{num} {ename}\n      game: {}\n      fsim: {}",
                        sim.model.signals.format(&expected),
                        sim.model.signals.format(&actual)
                    ));
                }
            } else if let Some(lit) = entry.get("lit").and_then(Value::as_bool) {
                let color = entry.get("color").and_then(Value::as_array).map(|c| {
                    let ch = |i: usize| (c[i].as_f64().unwrap_or(0.0) * 255.0).round() as u8;
                    [ch(0), ch(1), ch(2)]
                });
                let expected = LampState { lit, color };
                let actual = match (k as i64) - 1 {
                    t if t >= 0 => lamps[t as usize][e],
                    t => pre_lamps.get((warmup + t) as usize).and_then(|l| l[e]).or_else(|| sim.initial_lamp(e)),
                };
                res.checks += 1;
                if Some(expected) != actual {
                    res.mismatches.push(format!("row {k} #{num} lamp\n      game: {expected:?}\n      fsim: {actual:?}"));
                }
            }
        }
    }
    // Every combinator/lamp should have been recorded; catch harness mapping gaps.
    let recorded: usize = rows.first().and_then(Value::as_object).map(|o| o.len()).unwrap_or(0);
    let expected_count = sim
        .model
        .entities
        .iter()
        .filter(|e| e.behavior.is_combinator() || matches!(e.behavior, Behavior::Lamp(_)))
        .count();
    if n > 0 && recorded != expected_count {
        res.mismatches.push(format!("game recorded {recorded} entities, blueprint has {expected_count}"));
    }
    Ok(res)
}
