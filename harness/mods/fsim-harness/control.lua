-- fsim harness: builds generated cases, runs them from a clean start, and dumps what the game
-- computed so fsim can be checked against it. cases.lua is written by harness/run.py.
--
-- Timeline: tick 1 builds every case with its constant combinators switched off. At tick T0 the
-- harness switches them on (if the blueprint had them on). Row k of a recording is read in on_tick
-- at game tick T0 + k.
--
-- Mode "bench" builds and switches on the same way but records nothing, so --benchmark-verbose
-- timings show the blueprint's own cost. It writes fsim/bench.json at T0 (power check, counts).

local config = require("cases")

if config.mode ~= "run" and config.mode ~= "bench" then
  script.on_event(defines.events.on_tick, require(config.mode))
  return
end

local MARGIN = 24

local function make_surface(x0, x1, y0, y1)
  local s = game.create_surface("fsim", {
    default_enable_all_autoplace_controls = false,
    autoplace_settings = {
      entity = { treat_missing_as_default = false, settings = {} },
      decorative = { treat_missing_as_default = false, settings = {} },
    },
    no_enemies_mode = true,
  })
  s.always_day = false
  s.freeze_daytime = true
  s.daytime = 0.5
  for cx = math.floor(x0 / 32), math.floor(x1 / 32) do
    for cy = math.floor(y0 / 32), math.floor(y1 / 32) do s.request_to_generate_chunks({ cx * 32 + 16, cy * 32 + 16 }, 0) end
  end
  s.force_generate_chunk_requests()
  local tiles = {}
  for x = x0, x1 do
    for y = y0, y1 do tiles[#tiles + 1] = { name = "lab-dark-1", position = { x, y } } end
  end
  s.set_tiles(tiles)
  return s
end

local function key(x, y) return string.format("%.1f,%.1f", x, y) end

local function build_case(s, c, origin)
  local inv = game.create_inventory(1)
  local stack = inv[1]
  assert(stack.import_stack(c.bp) == 0, "blueprint import failed for " .. c.name)
  local bp_entities = stack.get_blueprint_entities() or {}
  local ghosts = stack.build_blueprint { surface = s, force = "player", position = origin,
    build_mode = defines.build_mode.superforced, skip_fog_of_war = false }
  local built = {}
  for _, g in pairs(ghosts) do
    if g.valid then
      local _, ent = g.revive()
      if ent then built[#built + 1] = ent end
    end
  end
  inv.destroy()
  assert(#built == #bp_entities, string.format("%s: built %d of %d entities", c.name, #built, #bp_entities))

  -- Blueprint positions and built positions differ by one offset; find it from the minimum corner.
  local function min_corner(list, pos_of)
    local mx, my = math.huge, math.huge
    for _, e in pairs(list) do
      local p = pos_of(e)
      if p.x < mx then mx = p.x end
      if p.y < my then my = p.y end
    end
    return mx, my
  end
  local bx, by = min_corner(bp_entities, function(e) return e.position end)
  local wx, wy = min_corner(built, function(e) return e.position end)
  local by_pos = {}
  for _, e in pairs(built) do by_pos[key(e.position.x, e.position.y)] = e end

  local entities, constants = {}, {}
  for _, be in pairs(bp_entities) do
    local e = by_pos[key(be.position.x - bx + wx, be.position.y - by + wy)]
    assert(e and e.name == be.name, c.name .. ": could not map entity " .. be.entity_number)
    entities[be.entity_number] = e
    if e.type == "constant-combinator" then
      local cb = e.get_control_behavior()
      constants[#constants + 1] = { entity = e, enabled = cb.enabled }
      cb.enabled = false
    end
  end

  -- Power: copper-link substations within reach, and put an energy interface inside every
  -- substation's supply area that has room for one (a substation inside a solid lamp screen
  -- doesn't, and gets power through its copper links instead).
  local subs = {}
  for _, e in pairs(built) do
    if e.name == "substation" then subs[#subs + 1] = e end
  end
  local copper = defines.wire_connector_id.pole_copper
  for i = 1, #subs do
    for j = i + 1, #subs do
      local dx, dy = subs[i].position.x - subs[j].position.x, subs[i].position.y - subs[j].position.y
      if dx * dx + dy * dy <= 18 * 18 then
        subs[i].get_wire_connector(copper, true).connect_to(subs[j].get_wire_connector(copper, true), false)
      end
    end
    local pos = s.find_non_colliding_position("electric-energy-interface", subs[i].position, 8, 1)
    if pos then
      local eei = s.create_entity { name = "electric-energy-interface", position = pos, force = "player" }
      eei.power_production = 1e9
      eei.electric_buffer_size = 1e10
    end
  end
  return { name = c.name, ticks = c.ticks, entities = entities, constants = constants, rows = {} }
end

local function read_signals(list)
  local outv = {}
  for _, sv in pairs(list or {}) do
    local s = sv.signal
    outv[#outv + 1] = { s.type or "item", s.name, s.quality or "normal", sv.count }
  end
  return outv
end

local function record(run, k)
  local row = {}
  for num, e in pairs(run.entities) do
    local t = e.type
    if t == "arithmetic-combinator" or t == "decider-combinator" or t == "selector-combinator" then
      row[tostring(num)] = { out = read_signals(e.get_control_behavior().signals_last_tick) }
    elseif t == "lamp" then
      local cb = e.get_control_behavior()
      local col = cb and cb.color
      local entry = { lit = not (cb and cb.disabled) }
      if col then entry.color = { col.r, col.g, col.b } end
      row[tostring(num)] = entry
    end
  end
  run.rows[k + 1] = row
end

script.on_event(defines.events.on_tick, function(e)
  local tick = e.tick
  if tick == 1 then
    -- Lay the cases out left to right by their sizes, with tiles under all of them.
    local spans, cursor, half_h = {}, 0, 16
    local inv = game.create_inventory(1)
    for i, c in ipairs(config.cases) do
      assert(inv[1].import_stack(c.bp) == 0, "blueprint import failed for " .. c.name)
      local x0, x1, y0, y1 = math.huge, -math.huge, math.huge, -math.huge
      for _, e in pairs(inv[1].get_blueprint_entities() or {}) do
        x0, x1 = math.min(x0, e.position.x), math.max(x1, e.position.x)
        y0, y1 = math.min(y0, e.position.y), math.max(y1, e.position.y)
      end
      local w = (x1 > x0) and (x1 - x0) or 0
      half_h = math.max(half_h, ((y1 > y0) and (y1 - y0) or 0) / 2)
      spans[i] = math.floor(cursor + MARGIN + w / 2)
      cursor = cursor + w + 2 * MARGIN
    end
    inv.destroy()
    local hh = math.ceil(half_h) + MARGIN
    local s = make_surface(-MARGIN, math.ceil(cursor) + MARGIN, -hh, hh)
    storage.runs = {}
    for i, c in ipairs(config.cases) do
      storage.runs[i] = build_case(s, c, { spans[i], 0 })
    end
    storage.t0 = 1 + config.warmup
    return
  end
  if not storage.t0 or tick < storage.t0 then return end
  local k = tick - storage.t0
  if k == 0 then
    for _, run in pairs(storage.runs) do
      for _, c in pairs(run.constants) do c.entity.get_control_behavior().enabled = c.enabled end
      -- Anything unpowered would silently look like a semantics difference; report it instead.
      run.unpowered = {}
      for num, ent in pairs(run.entities) do
        local needs_power = ent.type == "lamp" or (ent.type:find("combinator") and ent.type ~= "constant-combinator")
        if needs_power and ent.status == defines.entity_status.no_power then
          run.unpowered[#run.unpowered + 1] = num
        end
      end
    end
  end
  if config.mode == "bench" then
    if k == 0 then
      local info = { t0 = storage.t0, cases = {} }
      for _, run in pairs(storage.runs) do
        local n = 0
        for _ in pairs(run.entities) do n = n + 1 end
        info.cases[run.name] = { entities = n, unpowered = #run.unpowered }
      end
      helpers.write_file("fsim/bench.json", helpers.table_to_json(info), false)
    end
    return
  end
  local active = false
  for _, run in pairs(storage.runs) do
    if k <= run.ticks then
      record(run, k)
      active = true
    end
  end
  if not active and not storage.written then
    local dump = { t0 = storage.t0, cases = {} }
    dump.unpowered = {}
    for _, run in pairs(storage.runs) do
      dump.cases[run.name] = run.rows
      if #run.unpowered > 0 then dump.unpowered[run.name] = run.unpowered end
    end
    helpers.write_file("fsim/dump.json", helpers.table_to_json(dump), false)
    storage.written = true
  end
end)
