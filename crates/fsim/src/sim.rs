//! Tick-accurate circuit simulation.
//!
//! Timing model (verified against the game by the `timing` conformance case):
//!   N(t) = constants + O(t-1)       every network is the wrapping sum of what feeds it
//!   O(t) = f(N(t))                  every combinator reads its inputs, outputs next tick
//!   lamp color(t) = color(N(t)),  lamp lit(t) = condition(N(t-1))
//! Simulation tick t corresponds to game tick `start_tick + t`, which is what a selector in
//! time mode outputs.

use std::borrow::Cow;

use crate::model::{Arith, Behavior, ColorMode, Cond, Decider, DOut, Lamp, Model, Nets, Op, Operand, Selector, Sig};
use crate::signals::{add, get, Accum, SigVec};

/// Connector ids used in blueprint wires.
pub const RED_IN: u32 = 1;
pub const GREEN_IN: u32 = 2;
pub const RED_OUT: u32 = 3;
pub const GREEN_OUT: u32 = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LampState {
    pub lit: bool,
    pub color: Option<[u8; 3]>,
}

pub struct Sim {
    pub model: Model,
    /// Network id per entity per connector 1..=4 (index 0 unused).
    pub conn: Vec<[Option<usize>; 5]>,
    pub net_count: usize,
    /// For each network, the entities whose output feeds it.
    sources: Vec<Vec<usize>>,
    pub outputs: Vec<SigVec>,
    pub nets: Vec<SigVec>,
    pub lamps: Vec<Option<LampState>>,
    lamp_cond_prev: Vec<bool>,
    pub tick: i64,
    pub start_tick: i64,
}

fn empty() -> &'static SigVec {
    static EMPTY: SigVec = Vec::new();
    &EMPTY
}

impl Sim {
    pub fn new(model: Model, start_tick: i64) -> Sim {
        let n = model.entities.len();
        let index: std::collections::HashMap<u32, usize> =
            model.entities.iter().enumerate().map(|(i, e)| (e.number, i)).collect();

        // Union-find over (entity, connector) nodes.
        let node = |e: usize, c: u32| e * 5 + c as usize;
        let mut parent: Vec<usize> = (0..n * 5).collect();
        fn find(p: &mut [usize], mut x: usize) -> usize {
            while p[x] != x {
                p[x] = p[p[x]];
                x = p[x];
            }
            x
        }
        let mut used = vec![false; n * 5];
        let mut model = model;
        let mut dropped = Vec::new();
        for w in &model.wires {
            let (Some(&a), Some(&b)) = (index.get(&w[0]), index.get(&w[2])) else { continue };
            if !(1..=4).contains(&w[1]) || !(1..=4).contains(&w[3]) {
                continue; // copper
            }
            let (ea, eb) = (&model.entities[a], &model.entities[b]);
            let dist = footprint_gap(ea, eb);
            let reach = wire_reach(&ea.name).min(wire_reach(&eb.name));
            if dist > reach + 1e-9 {
                // The game silently refuses to build wires longer than the shorter reach.
                dropped.push(format!(
                    "wire #{} -> #{} spans a {dist:.2} tile gap, over the {reach} tile reach; the game drops it",
                    ea.number, eb.number
                ));
                continue;
            }
            let (na, nb) = (node(a, w[1]), node(b, w[3]));
            used[na] = true;
            used[nb] = true;
            let (ra, rb) = (find(&mut parent, na), find(&mut parent, nb));
            parent[ra] = rb;
        }
        model.warnings.extend(dropped);
        let mut net_of_root = std::collections::HashMap::new();
        let mut conn = vec![[None; 5]; n];
        for e in 0..n {
            for c in 1..=4u32 {
                let x = node(e, c);
                if used[x] {
                    let r = find(&mut parent, x);
                    let next = net_of_root.len();
                    conn[e][c as usize] = Some(*net_of_root.entry(r).or_insert(next));
                }
            }
        }
        let net_count = net_of_root.len();
        let mut sources = vec![Vec::new(); net_count];
        for (e, ent) in model.entities.iter().enumerate() {
            let outs: &[usize] = match ent.behavior {
                Behavior::Constant { .. } | Behavior::Container => &[1, 2],
                _ if ent.behavior.is_combinator() => &[3, 4],
                _ => &[],
            };
            for &c in outs {
                if let Some(net) = conn[e][c] {
                    sources[net].push(e);
                }
            }
        }
        let mut outputs = vec![SigVec::new(); n];
        for (e, ent) in model.entities.iter().enumerate() {
            if let Behavior::Constant { out, enabled: true } = &ent.behavior {
                outputs[e] = out.clone();
            }
        }
        let lamps = vec![None; n];
        let lamp_cond_prev = model
            .entities
            .iter()
            .map(|e| match &e.behavior {
                Behavior::Lamp(l) => lamp_enabled(l, empty()),
                _ => false,
            })
            .collect();
        Sim {
            model,
            conn,
            net_count,
            sources,
            outputs,
            nets: vec![SigVec::new(); net_count],
            lamps,
            lamp_cond_prev,
            tick: -1,
            start_tick,
        }
    }

    /// Switches every constant combinator on or off (as the blueprint has it when `on`).
    pub fn set_constants(&mut self, on: bool) {
        for e in 0..self.model.entities.len() {
            if let Behavior::Constant { enabled, .. } = self.model.entities[e].behavior {
                self.set_constant_enabled(e, on && enabled);
            }
        }
    }

    pub fn set_constant_enabled(&mut self, e: usize, on: bool) {
        if let Behavior::Constant { out, .. } = &self.model.entities[e].behavior {
            self.outputs[e] = if on { out.clone() } else { SigVec::new() };
        }
    }

    /// Puts `count` of an item into a chest (negative takes them out), as a script in on_tick
    /// would: networks see it from the next `step`.
    pub fn insert(&mut self, e: usize, item: &str, count: i32) -> Result<(), String> {
        if !matches!(self.model.entities[e].behavior, Behavior::Container) {
            return Err(format!("entity #{} ({}) is not a chest", self.model.entities[e].number, self.model.entities[e].name));
        }
        let id = self.model.signals.intern(crate::signals::SignalKey::new("item", item, "normal"));
        let have = get(&self.outputs[e], id);
        if have + count < 0 {
            return Err(format!("chest #{} has {have} {item}, can't take {}", self.model.entities[e].number, -count));
        }
        self.outputs[e] = add(&self.outputs[e], &vec![(id, count)]);
        Ok(())
    }

    /// Applies the inserts scheduled for simulation tick `t` (a list of [t, entity_number, item,
    /// count], the format of a case's `fsim.inserts` and of `fsim run --input`).
    pub fn apply_inserts(&mut self, schedule: &serde_json::Value, t: i64) -> anyhow::Result<()> {
        for ins in schedule.as_array().into_iter().flatten() {
            let a = ins.as_array().ok_or_else(|| anyhow::anyhow!("insert entry is not a list"))?;
            if a.first().and_then(|v| v.as_i64()) != Some(t) {
                continue;
            }
            let num = a[1].as_u64().unwrap_or(0) as u32;
            let e = self.model.entity_index(num).ok_or_else(|| anyhow::anyhow!("insert: no entity {num}"))?;
            self.insert(e, a[2].as_str().unwrap_or(""), a[3].as_i64().unwrap_or(0) as i32).map_err(anyhow::Error::msg)?;
        }
        Ok(())
    }

    fn input(&self, e: usize, c: usize) -> &SigVec {
        match self.conn[e][c] {
            Some(n) => &self.nets[n],
            None => empty(),
        }
    }

    /// Advances one tick: networks take last tick's outputs, then everything evaluates.
    pub fn step(&mut self) {
        self.tick += 1;
        for n in 0..self.net_count {
            let mut v = SigVec::new();
            for &e in &self.sources[n] {
                v = add(&v, &self.outputs[e]);
            }
            self.nets[n] = v;
        }
        let game_tick = self.start_tick + self.tick;
        for e in 0..self.model.entities.len() {
            let behavior = &self.model.entities[e].behavior;
            match behavior {
                Behavior::Arith(a) => {
                    let out = eval_arith(a, self.input(e, 1), self.input(e, 2));
                    self.outputs[e] = out;
                }
                Behavior::Decider(d) => {
                    let out = eval_decider(d, self.input(e, 1), self.input(e, 2));
                    self.outputs[e] = out;
                }
                Behavior::Selector(s) => {
                    let out = eval_selector(s, self.input(e, 1), self.input(e, 2), game_tick);
                    self.outputs[e] = out;
                }
                Behavior::Lamp(_) if self.conn[e][1].is_none() && self.conn[e][2].is_none() => {
                    // Not connected: the control behavior does nothing, the lamp just shines.
                    self.lamps[e] = Some(LampState { lit: true, color: None });
                }
                Behavior::Lamp(l) => {
                    let merged = add(self.input(e, 1), self.input(e, 2));
                    let lit = self.lamp_cond_prev[e];
                    self.lamp_cond_prev[e] = lamp_enabled(l, &merged);
                    let color = if lit { l.colors.as_ref().and_then(|m| lamp_color(m, &merged)) } else { None };
                    self.lamps[e] = Some(LampState { lit, color });
                }
                _ => {}
            }
        }
    }

    /// Lamp state as it would be shown before the first tick.
    pub fn initial_lamp(&self, e: usize) -> Option<LampState> {
        match &self.model.entities[e].behavior {
            Behavior::Lamp(_) if self.conn[e][1].is_none() && self.conn[e][2].is_none() => {
                Some(LampState { lit: true, color: None })
            }
            Behavior::Lamp(l) => {
                let lit = lamp_enabled(l, empty());
                Some(LampState { lit, color: if lit { l.colors.as_ref().and_then(|m| lamp_color(m, empty())) } else { None } })
            }
            _ => None,
        }
    }
}

/// Wire length as the game checks it: the straight-line gap between the two tile footprints
/// (0 when they touch), except between two electric poles, where it is the distance between their
/// centers. Verified by the `wire_reach` and `pole_reach` conformance cases.
pub fn footprint_gap(a: &crate::model::Entity, b: &crate::model::Entity) -> f64 {
    if is_pole(&a.name) && is_pole(&b.name) {
        return ((a.x - b.x).powi(2) + (a.y - b.y).powi(2)).sqrt();
    }
    let ((aw, ah), (bw, bh)) = (a.size(), b.size());
    let dx = ((a.x - b.x).abs() - (aw + bw) / 2.0).max(0.0);
    let dy = ((a.y - b.y).abs() - (ah + bh) / 2.0).max(0.0);
    (dx * dx + dy * dy).sqrt()
}

pub fn is_pole(name: &str) -> bool {
    matches!(name, "small-electric-pole" | "medium-electric-pole" | "big-electric-pole" | "substation")
}

/// Circuit wire reach in tiles (normal quality), from the base game prototypes. A wire's reach is
/// the shorter of its two ends' (verified: a big pole can't reach a constant combinator 11 away).
pub fn wire_reach(name: &str) -> f64 {
    match name {
        "small-electric-pole" => 7.5,
        "medium-electric-pole" => 9.0,
        "big-electric-pole" => 32.0,
        "substation" => 18.0,
        _ => 9.0,
    }
}

/// The values an operand reads from the selected wires.
fn merged<'a>(red: &'a SigVec, green: &'a SigVec, n: Nets) -> Cow<'a, SigVec> {
    match (n.red, n.green) {
        (true, true) => Cow::Owned(add(red, green)),
        (true, false) => Cow::Borrowed(red),
        (false, true) => Cow::Borrowed(green),
        (false, false) => Cow::Owned(SigVec::new()),
    }
}

fn value(red: &SigVec, green: &SigVec, id: u32, n: Nets) -> i32 {
    let mut v = 0i32;
    if n.red {
        v = v.wrapping_add(get(red, id));
    }
    if n.green {
        v = v.wrapping_add(get(green, id));
    }
    v
}

/// Union of the signals present on the wires read by each `Each` operand.
fn lanes(red: &SigVec, green: &SigVec, operands: &[Operand]) -> Vec<u32> {
    let mut ids: Vec<u32> = Vec::new();
    for op in operands {
        if let Operand::Sig(Sig::Each, n) = op {
            ids.extend(merged(red, green, *n).iter().map(|e| e.0));
        }
    }
    ids.sort_unstable();
    ids.dedup();
    ids
}

fn scalar(op: &Operand, red: &SigVec, green: &SigVec, lane: Option<u32>) -> i32 {
    match *op {
        Operand::None => 0,
        Operand::Const(c) => c,
        Operand::Sig(Sig::Id(id), n) => value(red, green, id, n),
        Operand::Sig(Sig::Each, n) => lane.map(|l| value(red, green, l, n)).unwrap_or(0),
        Operand::Sig(_, _) => 0,
    }
}

pub fn apply(op: Op, a: i32, b: i32) -> i32 {
    match op {
        Op::Mul => a.wrapping_mul(b),
        Op::Add => a.wrapping_add(b),
        Op::Sub => a.wrapping_sub(b),
        // Division by zero and the INT_MIN / -1 overflow both give 0.
        Op::Div => {
            if b == 0 || (a == i32::MIN && b == -1) {
                0
            } else {
                a / b
            }
        }
        Op::Mod => {
            if b == 0 || (a == i32::MIN && b == -1) {
                0
            } else {
                a % b
            }
        }
        Op::Pow => {
            if b < 0 {
                match a {
                    1 => 1,
                    -1 => {
                        if b % 2 == 0 {
                            1
                        } else {
                            -1
                        }
                    }
                    _ => 0,
                }
            } else {
                a.wrapping_pow(b as u32)
            }
        }
        // Shift amounts are masked to 5 bits; >> is arithmetic.
        Op::Shl => a.wrapping_shl(b as u32),
        Op::Shr => a.wrapping_shr(b as u32),
        Op::And => a & b,
        Op::Or => a | b,
        Op::Xor => a ^ b,
    }
}

pub fn eval_arith(a: &Arith, red: &SigVec, green: &SigVec) -> SigVec {
    let Some(out) = a.out else { return SigVec::new() };
    if a.first.is_each() || a.second.is_each() {
        let ls = lanes(red, green, &[a.first, a.second]);
        match out {
            Sig::Each => {
                let mut v = SigVec::with_capacity(ls.len());
                for l in ls {
                    let r = apply(a.op, scalar(&a.first, red, green, Some(l)), scalar(&a.second, red, green, Some(l)));
                    if r != 0 {
                        v.push((l, r));
                    }
                }
                v
            }
            Sig::Id(id) => {
                let mut sum = 0i32;
                for l in ls {
                    sum = sum.wrapping_add(apply(
                        a.op,
                        scalar(&a.first, red, green, Some(l)),
                        scalar(&a.second, red, green, Some(l)),
                    ));
                }
                if sum != 0 {
                    vec![(id, sum)]
                } else {
                    SigVec::new()
                }
            }
            _ => SigVec::new(),
        }
    } else {
        let r = apply(a.op, scalar(&a.first, red, green, None), scalar(&a.second, red, green, None));
        match out {
            Sig::Id(id) if r != 0 => vec![(id, r)],
            _ => SigVec::new(),
        }
    }
}

fn cond_holds(c: &Cond, red: &SigVec, green: &SigVec, lane: Option<u32>) -> bool {
    let rhs = scalar(&c.second, red, green, lane);
    match c.first {
        Operand::Sig(Sig::Everything, n) => merged(red, green, n).iter().all(|&(_, v)| c.cmp.holds(v, rhs)),
        Operand::Sig(Sig::Anything, n) => merged(red, green, n).iter().any(|&(_, v)| c.cmp.holds(v, rhs)),
        Operand::None => false,
        ref first => c.cmp.holds(scalar(first, red, green, lane), rhs),
    }
}

fn groups_hold(d: &Decider, red: &SigVec, green: &SigVec, lane: Option<u32>) -> bool {
    d.groups.iter().any(|g| g.iter().all(|c| cond_holds(c, red, green, lane)))
}

fn emit_scalar(outs: &[DOut], red: &SigVec, green: &SigVec, acc: &mut Accum) {
    for o in outs {
        match o.sig {
            Sig::Id(id) => acc.add(id, if o.copy { value(red, green, id, o.nets) } else { o.constant }),
            Sig::Everything => {
                for &(id, v) in merged(red, green, o.nets).iter() {
                    acc.add(id, if o.copy { v } else { o.constant });
                }
            }
            Sig::Anything => {
                // Observed: outputs the first input signal in signal order.
                if let Some(&(id, v)) = merged(red, green, o.nets).first() {
                    acc.add(id, if o.copy { v } else { o.constant });
                }
            }
            Sig::Each => {}
        }
    }
}

fn emit_lanes(outs: &[DOut], red: &SigVec, green: &SigVec, lanes: &[u32], acc: &mut Accum) {
    if lanes.is_empty() {
        return;
    }
    for o in outs {
        match o.sig {
            Sig::Each => {
                for &l in lanes {
                    acc.add(l, if o.copy { value(red, green, l, o.nets) } else { o.constant });
                }
            }
            // A named output under Each conditions: constant x matching lanes, or the sum of the
            // matching lanes' own values when copying.
            Sig::Id(id) => {
                let v = if o.copy {
                    lanes.iter().fold(0i32, |s, &l| s.wrapping_add(value(red, green, l, o.nets)))
                } else {
                    o.constant.wrapping_mul(lanes.len() as i32)
                };
                acc.add(id, v);
            }
            Sig::Everything | Sig::Anything => {}
        }
    }
}

pub fn eval_decider(d: &Decider, red: &SigVec, green: &SigVec) -> SigVec {
    let mut acc = Accum::default();
    if d.has_each {
        let operands: Vec<Operand> = d.groups.iter().flatten().flat_map(|c| [c.first, c.second]).collect();
        let (mut pass, mut fail) = (Vec::new(), Vec::new());
        for l in lanes(red, green, &operands) {
            if groups_hold(d, red, green, Some(l)) {
                pass.push(l);
            } else {
                fail.push(l);
            }
        }
        emit_lanes(&d.outputs, red, green, &pass, &mut acc);
        emit_lanes(&d.else_outputs, red, green, &fail, &mut acc);
    } else if groups_hold(d, red, green, None) {
        emit_scalar(&d.outputs, red, green, &mut acc);
    } else {
        emit_scalar(&d.else_outputs, red, green, &mut acc);
    }
    acc.finish()
}

pub fn eval_selector(s: &Selector, red: &SigVec, green: &SigVec, game_tick: i64) -> SigVec {
    let input = add(red, green);
    match s {
        Selector::Select { max, index, cmp } => {
            let idx = match *index {
                Operand::Const(c) => c,
                Operand::Sig(Sig::Id(id), _) => get(&input, id),
                _ => 0,
            };
            let mut sorted = input.clone();
            // Ties: signal order when selecting the max; the min list is exactly the max list
            // reversed, so its ties run in reverse signal order (both verified in game).
            if *max {
                sorted.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
            } else {
                sorted.sort_by(|a, b| a.1.cmp(&b.1).then(b.0.cmp(&a.0)));
            }
            let mut out: SigVec =
                sorted.into_iter().enumerate().filter(|(i, _)| cmp.holds(*i as i32, idx)).map(|(_, e)| e).collect();
            out.sort_unstable_by_key(|e| e.0);
            out
        }
        Selector::Count(Some(id)) if !input.is_empty() => vec![(*id, input.len() as i32)],
        Selector::Time { game_tick: Some(id) } if game_tick as i32 != 0 => vec![(*id, game_tick as i32)],
        _ => SigVec::new(),
    }
}

fn lamp_enabled(l: &Lamp, input: &SigVec) -> bool {
    match &l.condition {
        None => true,
        Some(c) => cond_holds(c, input, empty(), None),
    }
}

/// Lamp color-mapping priority (base game `signal_to_color_mapping`).
const COLOR_MAP: &[(&str, [u8; 3])] = &[
    ("signal-red", [255, 0, 0]),
    ("signal-green", [0, 255, 0]),
    ("signal-blue", [0, 0, 255]),
    ("signal-yellow", [255, 255, 0]),
    ("signal-pink", [255, 0, 255]),
    ("signal-cyan", [0, 255, 255]),
    ("signal-white", [255, 255, 255]),
    ("signal-grey", [128, 128, 128]),
    ("signal-black", [0, 0, 0]),
];

thread_local! {
    static COLOR_IDS: Vec<(u32, [u8; 3])> = {
        let t = crate::signals::SignalTable::vanilla();
        COLOR_MAP
            .iter()
            .filter_map(|(n, c)| t.find(&crate::signals::SignalKey::new("virtual", n, "normal")).map(|id| (id, *c)))
            .collect()
    };
}

/// The color a lit lamp shows; `None` means its default (non-circuit) light.
fn lamp_color(m: &ColorMode, input: &SigVec) -> Option<[u8; 3]> {
    let ch = |id: Option<u32>| id.map(|i| get(input, i).clamp(0, 255) as u8).unwrap_or(0);
    match *m {
        ColorMode::Packed(id) => {
            let v = id.map(|i| get(input, i)).unwrap_or(0);
            Some([(v >> 16) as u8, (v >> 8) as u8, v as u8])
        }
        ColorMode::Components { r, g, b } => Some([ch(r), ch(g), ch(b)]),
        ColorMode::Mapping => COLOR_IDS.with(|ids| ids.iter().find(|(id, _)| get(input, *id) > 0).map(|e| e.1)),
    }
}
