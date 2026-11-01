//! Lamp grid → images.

use std::path::Path;

use anyhow::Result;

use crate::model::Behavior;
use crate::sim::{LampState, Sim};

pub const BACKGROUND: [u8; 3] = [0, 0, 0];
pub const UNLIT: [u8; 3] = [40, 40, 40];
/// A lit lamp that isn't taking its color from the circuit.
pub const PLAIN_LIT: [u8; 3] = [255, 250, 220];

pub struct LampGrid {
    pub width: usize,
    pub height: usize,
    /// (entity index, x, y) in image pixels.
    cells: Vec<(usize, usize, usize)>,
}

impl LampGrid {
    pub fn new(sim: &Sim) -> Option<LampGrid> {
        let lamps: Vec<(usize, i64, i64)> = sim
            .model
            .entities
            .iter()
            .enumerate()
            .filter(|(_, e)| matches!(e.behavior, Behavior::Lamp(_)))
            .map(|(i, e)| (i, e.x.floor() as i64, e.y.floor() as i64))
            .collect();
        if lamps.is_empty() {
            return None;
        }
        let minx = lamps.iter().map(|l| l.1).min()?;
        let miny = lamps.iter().map(|l| l.2).min()?;
        let maxx = lamps.iter().map(|l| l.1).max()?;
        let maxy = lamps.iter().map(|l| l.2).max()?;
        Some(LampGrid {
            width: (maxx - minx + 1) as usize,
            height: (maxy - miny + 1) as usize,
            cells: lamps.into_iter().map(|(i, x, y)| (i, (x - minx) as usize, (y - miny) as usize)).collect(),
        })
    }

    pub fn lamp_count(&self) -> usize {
        self.cells.len()
    }

    /// The current frame, RGB, one pixel per lamp tile.
    pub fn frame(&self, sim: &Sim) -> Vec<u8> {
        let mut rgb = vec![0u8; self.width * self.height * 3];
        for px in rgb.chunks_mut(3) {
            px.copy_from_slice(&BACKGROUND);
        }
        for &(e, x, y) in &self.cells {
            let state = sim.lamps[e].or_else(|| sim.initial_lamp(e));
            let c = match state {
                Some(LampState { lit: true, color: Some(c) }) => c,
                Some(LampState { lit: true, color: None }) => PLAIN_LIT,
                _ => UNLIT,
            };
            let i = (y * self.width + x) * 3;
            rgb[i..i + 3].copy_from_slice(&c);
        }
        rgb
    }
}

pub fn upscale(rgb: &[u8], w: usize, h: usize, s: usize) -> Vec<u8> {
    let mut out = vec![0u8; w * s * h * s * 3];
    for y in 0..h * s {
        for x in 0..w * s {
            let src = ((y / s) * w + x / s) * 3;
            let dst = (y * w * s + x) * 3;
            out[dst..dst + 3].copy_from_slice(&rgb[src..src + 3]);
        }
    }
    out
}

pub fn write_png(path: &Path, rgb: &[u8], w: usize, h: usize) -> Result<()> {
    let file = std::io::BufWriter::new(std::fs::File::create(path)?);
    let mut enc = png::Encoder::new(file, w as u32, h as u32);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header()?.write_image_data(rgb)?;
    Ok(())
}

pub struct GifWriter {
    enc: gif::Encoder<std::io::BufWriter<std::fs::File>>,
    w: u16,
    h: u16,
    delay: u16,
}

impl GifWriter {
    /// `delay` is the frame time in hundredths of a second.
    pub fn create(path: &Path, w: usize, h: usize, delay: u16) -> Result<GifWriter> {
        let file = std::io::BufWriter::new(std::fs::File::create(path)?);
        let mut enc = gif::Encoder::new(file, w as u16, h as u16, &[])?;
        enc.set_repeat(gif::Repeat::Infinite)?;
        Ok(GifWriter { enc, w: w as u16, h: h as u16, delay })
    }

    pub fn push(&mut self, rgb: &[u8]) -> Result<()> {
        let mut frame = gif::Frame::from_rgb_speed(self.w, self.h, rgb, 10);
        frame.delay = self.delay;
        self.enc.write_frame(&frame)?;
        Ok(())
    }
}
