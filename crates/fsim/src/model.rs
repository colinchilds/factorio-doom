//! Blueprint decoding and parsing into a simulation model.

use std::io::Read;

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine;
use serde_json::Value;

use crate::signals::{SigVec, SignalKey, SignalTable};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sig {
    Id(u32),
    Each,
    Everything,
    Anything,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Nets {
    pub red: bool,
    pub green: bool,
}

impl Nets {
    pub const BOTH: Nets = Nets { red: true, green: true };
}

#[derive(Clone, Copy, Debug)]
pub enum Operand {
    None,
    Const(i32),
    Sig(Sig, Nets),
}

impl Operand {
    pub fn is_each(&self) -> bool {
        matches!(self, Operand::Sig(Sig::Each, _))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmp {
    Lt,
    Gt,
    Le,
    Ge,
    Eq,
    Ne,
}

impl Cmp {
    pub fn parse(s: Option<&str>) -> Result<Cmp> {
        Ok(match s.unwrap_or("<") {
            "<" => Cmp::Lt,
            ">" => Cmp::Gt,
            "<=" | "≤" => Cmp::Le,
            ">=" | "≥" => Cmp::Ge,
            "=" => Cmp::Eq,
            "!=" | "≠" => Cmp::Ne,
            other => bail!("unknown comparator {other:?}"),
        })
    }

    pub fn holds(self, a: i32, b: i32) -> bool {
        match self {
            Cmp::Lt => a < b,
            Cmp::Gt => a > b,
            Cmp::Le => a <= b,
            Cmp::Ge => a >= b,
            Cmp::Eq => a == b,
            Cmp::Ne => a != b,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Cond {
    pub first: Operand,
    pub second: Operand,
    pub cmp: Cmp,
}

#[derive(Clone, Debug)]
pub struct DOut {
    pub sig: Sig,
    pub copy: bool,
    pub constant: i32,
    pub nets: Nets,
}

#[derive(Clone, Debug)]
pub struct Decider {
    /// OR of AND-groups: AND binds tighter than OR (verified in game).
    pub groups: Vec<Vec<Cond>>,
    pub outputs: Vec<DOut>,
    pub else_outputs: Vec<DOut>,
    pub has_each: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Mul,
    Div,
    Add,
    Sub,
    Mod,
    Pow,
    Shl,
    Shr,
    And,
    Or,
    Xor,
}

#[derive(Clone, Debug)]
pub struct Arith {
    pub first: Operand,
    pub second: Operand,
    pub op: Op,
    pub out: Option<Sig>,
}

#[derive(Clone, Debug)]
pub enum Selector {
    Select { max: bool, index: Operand, cmp: Cmp },
    Count(Option<u32>),
    Time { game_tick: Option<u32> },
    /// Quality transfer with a static quality and destination Each: every input signal is output at
    /// that quality (signals that differ only in quality add up). `map[id]` is the id at the
    /// target quality (filled in after parsing, when every signal id is known).
    QualityTransfer { quality: String, map: Vec<u32> },
    /// Quality filter: only the input signals whose quality passes (`keep[id]`).
    QualityFilter { quality: String, cmp: Cmp, keep: Vec<bool> },
    Unsupported(String),
}

#[derive(Clone, Debug)]
pub enum ColorMode {
    Mapping,
    Components { r: Option<u32>, g: Option<u32>, b: Option<u32> },
    Packed(Option<u32>),
}

#[derive(Clone, Debug)]
pub struct Lamp {
    pub condition: Option<Cond>,
    pub colors: Option<ColorMode>,
}

#[derive(Clone, Debug)]
pub enum Behavior {
    Constant { out: SigVec, enabled: bool },
    Arith(Arith),
    Decider(Decider),
    Selector(Selector),
    Lamp(Lamp),
    /// A chest: its contents (`Sim::insert`) are on its wires like a constant combinator's
    /// signals, visible on the network the tick after a script inserts them (`chest_read`).
    Container,
    /// Poles, display panels and anything else: joins wires, sources nothing.
    Passive,
}

impl Behavior {
    pub fn is_combinator(&self) -> bool {
        matches!(self, Behavior::Arith(_) | Behavior::Decider(_) | Behavior::Selector(_))
    }
}

#[derive(Clone, Debug)]
pub struct Entity {
    pub number: u32,
    pub name: String,
    pub x: f64,
    pub y: f64,
    /// 16-way direction (0 north, 4 east, 8 south, 12 west).
    pub direction: u32,
    /// Entity quality ("normal" unless the blueprint says otherwise): pole wire reach depends on it.
    pub quality: String,
    pub behavior: Behavior,
}

impl Entity {
    /// Footprint (width, height) in tiles, accounting for rotation.
    pub fn size(&self) -> (f64, f64) {
        let (w, h) = match self.name.as_str() {
            "arithmetic-combinator" | "decider-combinator" | "selector-combinator" => (1.0, 2.0),
            "big-electric-pole" | "substation" => (2.0, 2.0),
            _ => (1.0, 1.0),
        };
        if self.direction % 8 == 4 {
            (h, w)
        } else {
            (w, h)
        }
    }
}

pub struct Model {
    pub label: String,
    pub entities: Vec<Entity>,
    pub wires: Vec<[u32; 4]>,
    pub signals: SignalTable,
    pub warnings: Vec<String>,
    /// Extra settings carried in conformance case files (`"fsim": {...}`).
    pub meta: Value,
}

impl Model {
    pub fn entity_index(&self, number: u32) -> Option<usize> {
        self.entities.iter().position(|e| e.number == number)
    }
}

/// Decodes a blueprint string ("0" + base64(zlib(json))).
pub fn decode_string(s: &str) -> Result<Value> {
    let s = s.trim();
    let body = s.strip_prefix('0').ok_or_else(|| anyhow!("blueprint string must start with version byte '0'"))?;
    let raw = base64::engine::general_purpose::STANDARD.decode(body).context("base64")?;
    let mut json = String::new();
    flate2::read::ZlibDecoder::new(&raw[..]).read_to_string(&mut json).context("zlib")?;
    Ok(serde_json::from_str(&json)?)
}

/// Loads a blueprint from a file holding either a blueprint string or blueprint JSON.
pub fn load_file(path: &std::path::Path) -> Result<Value> {
    let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    if text.trim_start().starts_with('{') {
        Ok(serde_json::from_str(&text)?)
    } else {
        decode_string(&text)
    }
}

const PASSIVE: &[&str] = &[
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
    "display-panel",
    "power-switch",
    "programmable-speaker",
];

pub fn parse(root: &Value) -> Result<Model> {
    let meta = root.get("fsim").cloned().unwrap_or(Value::Null);
    let bp = root.get("blueprint").ok_or_else(|| anyhow!("not a single blueprint (no 'blueprint' key)"))?;
    // A blueprint using Space Age qualities (signals or entities) runs with Space Age's signal order.
    let signals = if uses_dlc_quality(bp) { SignalTable::dlc() } else { SignalTable::vanilla() };
    let mut p = Parser { signals, warnings: Vec::new() };
    let mut entities = Vec::new();
    for e in bp.get("entities").and_then(Value::as_array).into_iter().flatten() {
        entities.push(p.entity(e)?);
    }
    // Quality selectors need every signal id at every quality: intern them all, then fill in the
    // tables (signals interned later, by scripted inserts, pass through unchanged).
    let qsel: Vec<String> = entities
        .iter()
        .filter_map(|e| match &e.behavior {
            Behavior::Selector(Selector::QualityTransfer { quality, .. }) => Some(quality.clone()),
            _ => None,
        })
        .collect();
    if entities.iter().any(|e| matches!(e.behavior, Behavior::Selector(Selector::QualityTransfer { .. } | Selector::QualityFilter { .. }))) {
        let n0 = p.signals.len() as u32;
        for id in 0..n0 {
            let k = p.signals.key(id).clone();
            for q in &qsel {
                p.signals.intern(crate::signals::SignalKey::new(&k.ty, &k.name, q));
            }
        }
        let n = p.signals.len() as u32;
        for e in entities.iter_mut() {
            match &mut e.behavior {
                Behavior::Selector(Selector::QualityTransfer { quality, map }) => {
                    *map = (0..n)
                        .map(|id| {
                            let k = p.signals.key(id).clone();
                            p.signals.find(&crate::signals::SignalKey::new(&k.ty, &k.name, quality)).unwrap()
                        })
                        .collect();
                }
                Behavior::Selector(Selector::QualityFilter { quality, cmp, keep }) => {
                    let lq = crate::signals::quality_level(quality);
                    *keep = (0..n)
                        .map(|id| {
                            let k = p.signals.key(id);
                            match (*cmp, crate::signals::quality_level(&k.quality), lq) {
                                (Cmp::Eq, _, _) => k.quality == *quality,
                                (Cmp::Ne, _, _) => k.quality != *quality,
                                (c, Some(a), Some(b)) => c.holds(a, b),
                                _ => false,
                            }
                        })
                        .collect();
                }
                _ => {}
            }
        }
    }
    let mut wires = Vec::new();
    for w in bp.get("wires").and_then(Value::as_array).into_iter().flatten() {
        let a: Vec<u32> = w.as_array().into_iter().flatten().filter_map(Value::as_u64).map(|x| x as u32).collect();
        if a.len() != 4 {
            bail!("malformed wire {w}");
        }
        wires.push([a[0], a[1], a[2], a[3]]);
    }
    Ok(Model {
        label: bp.get("label").and_then(Value::as_str).unwrap_or("").to_string(),
        entities,
        wires,
        signals: p.signals,
        warnings: p.warnings,
        meta,
    })
}

fn uses_dlc_quality(v: &Value) -> bool {
    match v {
        Value::Object(m) => m.iter().any(|(k, x)| {
            (k == "quality" && matches!(x.as_str(), Some("uncommon" | "rare" | "epic" | "legendary"))) || uses_dlc_quality(x)
        }),
        Value::Array(a) => a.iter().any(uses_dlc_quality),
        _ => false,
    }
}

struct Parser {
    signals: SignalTable,
    warnings: Vec<String>,
}

fn nets(v: Option<&Value>) -> Nets {
    match v {
        None => Nets::BOTH,
        Some(v) => Nets {
            red: v.get("red").and_then(Value::as_bool).unwrap_or(true),
            green: v.get("green").and_then(Value::as_bool).unwrap_or(true),
        },
    }
}

fn int(v: Option<&Value>, default: i32) -> i32 {
    v.and_then(Value::as_i64).map(|x| x as i32).unwrap_or(default)
}

impl Parser {
    fn signal(&mut self, v: &Value) -> Result<Option<Sig>> {
        let Some(name) = v.get("name").and_then(Value::as_str) else { return Ok(None) };
        let ty = v.get("type").and_then(Value::as_str).unwrap_or("item");
        let quality = v.get("quality").and_then(Value::as_str).unwrap_or("normal");
        if ty == "virtual" {
            match name {
                "signal-each" => return Ok(Some(Sig::Each)),
                "signal-everything" => return Ok(Some(Sig::Everything)),
                "signal-anything" => return Ok(Some(Sig::Anything)),
                _ => {}
            }
        }
        let key = SignalKey::new(ty, name, quality);
        let id = match self.signals.find(&key) {
            Some(id) => id,
            // A known signal at a Space Age quality (the table is vanilla's: normal and
            // quality-unknown): fine, it just gets an id after the known ones.
            None if crate::signals::quality_level(quality).is_some()
                && self.signals.find(&SignalKey::new(ty, name, "normal")).is_some() =>
            {
                self.signals.intern(key)
            }
            None => {
                self.warnings.push(format!("signal {} is not in the game's signal list", key.short()));
                self.signals.intern(key)
            }
        };
        Ok(Some(Sig::Id(id)))
    }

    fn operand(&mut self, cond: &Value, signal: &str, networks: &str, constant: &str) -> Result<Operand> {
        if let Some(s) = cond.get(signal) {
            if let Some(sig) = self.signal(s)? {
                return Ok(Operand::Sig(sig, nets(cond.get(networks))));
            }
        }
        Ok(match cond.get(constant) {
            Some(c) => Operand::Const(int(Some(c), 0)),
            None => Operand::None,
        })
    }

    fn cond(&mut self, c: &Value) -> Result<Cond> {
        let first = self.operand(c, "first_signal", "first_signal_networks", "__none__")?;
        let mut second = self.operand(c, "second_signal", "second_signal_networks", "constant")?;
        if matches!(second, Operand::None) {
            second = Operand::Const(0);
        }
        Ok(Cond { first, second, cmp: Cmp::parse(c.get("comparator").and_then(Value::as_str))? })
    }

    fn outputs(&mut self, list: Option<&Value>) -> Result<Vec<DOut>> {
        let mut outs = Vec::new();
        for o in list.and_then(Value::as_array).into_iter().flatten() {
            let Some(sig) = o.get("signal").map(|s| self.signal(s)).transpose()?.flatten() else { continue };
            outs.push(DOut {
                sig,
                copy: o.get("copy_count_from_input").and_then(Value::as_bool).unwrap_or(true),
                constant: int(o.get("constant"), 1),
                nets: nets(o.get("networks")),
            });
        }
        Ok(outs)
    }

    fn entity(&mut self, e: &Value) -> Result<Entity> {
        let number = e.get("entity_number").and_then(Value::as_u64).ok_or_else(|| anyhow!("entity without number"))? as u32;
        let name = e.get("name").and_then(Value::as_str).unwrap_or("").to_string();
        let pos = e.get("position");
        let x = pos.and_then(|p| p.get("x")).and_then(Value::as_f64).unwrap_or(0.0);
        let y = pos.and_then(|p| p.get("y")).and_then(Value::as_f64).unwrap_or(0.0);
        let null = Value::Null;
        let cb = e.get("control_behavior").unwrap_or(&null);
        let direction = e.get("direction").and_then(Value::as_u64).unwrap_or(0) as u32;
        let behavior = self.behavior(&name, cb).with_context(|| format!("entity {number} ({name})"))?;
        let quality = e.get("quality").and_then(Value::as_str).unwrap_or("normal").to_string();
        Ok(Entity { number, name, x, y, direction, quality, behavior })
    }

    fn behavior(&mut self, name: &str, cb: &Value) -> Result<Behavior> {
        Ok(match name {
            "constant-combinator" => {
                let mut acc = crate::signals::Accum::default();
                for sec in cb.pointer("/sections/sections").and_then(Value::as_array).into_iter().flatten() {
                    if sec.get("active").and_then(Value::as_bool) == Some(false) {
                        continue;
                    }
                    for f in sec.get("filters").and_then(Value::as_array).into_iter().flatten() {
                        if let Some(Sig::Id(id)) = self.signal(f)? {
                            acc.add(id, int(f.get("count"), 0));
                        }
                    }
                }
                Behavior::Constant { out: acc.finish(), enabled: cb.get("is_on").and_then(Value::as_bool).unwrap_or(true) }
            }
            "arithmetic-combinator" => {
                let a = cb.get("arithmetic_conditions").unwrap_or(&Value::Null);
                let op = match a.get("operation").and_then(Value::as_str).unwrap_or("*") {
                    "*" => Op::Mul,
                    "/" => Op::Div,
                    "+" => Op::Add,
                    "-" => Op::Sub,
                    "%" => Op::Mod,
                    "^" => Op::Pow,
                    "<<" => Op::Shl,
                    ">>" => Op::Shr,
                    "AND" => Op::And,
                    "OR" => Op::Or,
                    "XOR" => Op::Xor,
                    other => bail!("unknown arithmetic operation {other:?}"),
                };
                let first = self.operand(a, "first_signal", "first_signal_networks", "first_constant")?;
                let second = self.operand(a, "second_signal", "second_signal_networks", "second_constant")?;
                let out = a.get("output_signal").map(|s| self.signal(s)).transpose()?.flatten();
                Behavior::Arith(Arith { first, second, op, out })
            }
            "decider-combinator" => {
                let d = cb.get("decider_conditions").unwrap_or(&Value::Null);
                let mut groups: Vec<Vec<Cond>> = Vec::new();
                for (i, c) in d.get("conditions").and_then(Value::as_array).into_iter().flatten().enumerate() {
                    let and = c.get("compare_type").and_then(Value::as_str) == Some("and");
                    let cond = self.cond(c)?;
                    if i == 0 || !and {
                        groups.push(vec![cond]);
                    } else {
                        groups.last_mut().unwrap().push(cond);
                    }
                }
                let has_each = groups.iter().flatten().any(|c| c.first.is_each() || c.second.is_each());
                let outputs = self.outputs(d.get("outputs"))?;
                let else_outputs = self.outputs(d.get("else_outputs"))?;
                Behavior::Decider(Decider { groups, outputs, else_outputs, has_each })
            }
            "selector-combinator" => {
                let op = cb.get("operation").and_then(Value::as_str).unwrap_or("select");
                let sig = |p: &mut Self, key: &str| -> Result<Option<u32>> {
                    Ok(match cb.get(key).map(|s| p.signal(s)).transpose()?.flatten() {
                        Some(Sig::Id(id)) => Some(id),
                        _ => None,
                    })
                };
                Behavior::Selector(match op {
                    "select" => {
                        let index = match cb.get("index_signal").map(|s| self.signal(s)).transpose()?.flatten() {
                            Some(s) => Operand::Sig(s, Nets::BOTH),
                            None => Operand::Const(int(cb.get("index_constant"), 0)),
                        };
                        Selector::Select {
                            max: cb.get("select_max").and_then(Value::as_bool).unwrap_or(true),
                            index,
                            cmp: Cmp::parse(Some(cb.get("index_comparator").and_then(Value::as_str).unwrap_or("=")))?,
                        }
                    }
                    "count" => Selector::Count(sig(self, "count_signal")?),
                    "time" => {
                        for k in ["day_tick_signal", "day_length_signal"] {
                            if cb.get(k).is_some() {
                                self.warnings.push(format!("selector time mode: {k} is not simulated"));
                            }
                        }
                        Selector::Time { game_tick: sig(self, "game_tick_signal")? }
                    }
                    "quality-transfer"
                        if cb.get("select_quality_from_signal").and_then(Value::as_bool) != Some(true)
                            && cb.pointer("/quality_destination_signal/name").and_then(Value::as_str) == Some("signal-each") =>
                    {
                        let q = cb.pointer("/quality_source_static/name").and_then(Value::as_str).unwrap_or("normal");
                        Selector::QualityTransfer { quality: q.to_string(), map: Vec::new() }
                    }
                    "quality-filter" => {
                        let f = cb.get("quality_filter").unwrap_or(&Value::Null);
                        let q = f.get("quality").and_then(Value::as_str).unwrap_or("normal");
                        let cmp = Cmp::parse(Some(f.get("comparator").and_then(Value::as_str).unwrap_or("=")))?;
                        Selector::QualityFilter { quality: q.to_string(), cmp, keep: Vec::new() }
                    }
                    other => {
                        self.warnings.push(format!("selector operation {other:?} is not simulated (outputs nothing)"));
                        Selector::Unsupported(other.to_string())
                    }
                })
            }
            "small-lamp" => {
                let condition = if cb.get("circuit_enabled").and_then(Value::as_bool) == Some(true) {
                    Some(self.cond(cb.get("circuit_condition").unwrap_or(&Value::Null))?)
                } else {
                    None
                };
                let colors = if cb.get("use_colors").and_then(Value::as_bool) == Some(true) {
                    let sig = |p: &mut Self, key: &str| -> Result<Option<u32>> {
                        Ok(match cb.get(key).map(|s| p.signal(s)).transpose()?.flatten() {
                            Some(Sig::Id(id)) => Some(id),
                            _ => None,
                        })
                    };
                    Some(match cb.get("color_mode").and_then(Value::as_u64).unwrap_or(0) {
                        0 => ColorMode::Mapping,
                        1 => ColorMode::Components {
                            r: sig(self, "red_signal")?,
                            g: sig(self, "green_signal")?,
                            b: sig(self, "blue_signal")?,
                        },
                        2 => ColorMode::Packed(sig(self, "rgb_signal")?),
                        m => bail!("unknown lamp color_mode {m}"),
                    })
                } else {
                    None
                };
                Behavior::Lamp(Lamp { condition, colors })
            }
            "wooden-chest" | "iron-chest" | "steel-chest" => Behavior::Container,
            n => {
                if !PASSIVE.contains(&n) && !cb.is_null() {
                    self.warnings.push(format!("{n}: control behavior is not simulated"));
                }
                Behavior::Passive
            }
        })
    }
}
