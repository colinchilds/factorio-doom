-- Signal-order discovery: put every valid signal (value 1) on one network, pass it through an
-- arithmetic combinator, and record the order the game lists them in. That order is the game's
-- internal signal order, which also decides selector-combinator ties.

local types = {
  { "item", prototypes.item }, { "fluid", prototypes.fluid }, { "virtual", prototypes.virtual_signal },
  { "recipe", prototypes.recipe }, { "entity", prototypes.entity }, { "space-location", prototypes.space_location },
  { "asteroid-chunk", prototypes.asteroid_chunk }, { "quality", prototypes.quality },
}
local SKIP = { ["signal-each"] = true, ["signal-everything"] = true, ["signal-anything"] = true }

return function(e)
  local s = game.surfaces.nauvis
  if e.tick == 1 then
    s.request_to_generate_chunks({ 0, 0 }, 1)
    s.force_generate_chunk_requests()
    local tiles = {}
    for x = -40, 40 do for y = -10, 10 do tiles[#tiles + 1] = { name = "lab-dark-1", position = { x, y } } end end
    s.set_tiles(tiles)
    for _, ent in pairs(s.find_entities_filtered { area = { { -40, -10 }, { 40, 10 } } }) do
      if ent.type ~= "character" then ent.destroy() end
    end
    local qualities = {}
    for name in pairs(prototypes.quality) do qualities[#qualities + 1] = name end
    local calc = s.create_entity { name = "arithmetic-combinator", position = { 0.5, 3 }, force = "player" }
    calc.get_control_behavior().parameters = { first_signal = { type = "virtual", name = "signal-each" },
      operation = "*", second_constant = 1, output_signal = { type = "virtual", name = "signal-each" } }
    local sub = s.create_entity { name = "substation", position = { 4, 6 }, force = "player" }
    local eei = s.create_entity { name = "electric-energy-interface", position = { 8, 6 }, force = "player" }
    eei.power_production = 1e9
    eei.electric_buffer_size = 1e10
    local cc, section, slot, made = nil, nil, 1001, 0
    local W = defines.wire_connector_id
    local prev = calc.get_wire_connector(W.combinator_input_red, true)
    for _, t in ipairs(types) do
      for name in pairs(t[2]) do
        if not SKIP[name] then
          for _, q in ipairs(qualities) do
            if slot > 1000 then
              made = made + 1
              cc = s.create_entity { name = "constant-combinator", position = { 0.5 + made, 0.5 }, force = "player" }
              local conn = cc.get_wire_connector(W.circuit_red, true)
              assert(conn.connect_to(prev), "wire failed")
              prev = conn
              local ccb = cc.get_control_behavior() or cc.get_or_create_control_behavior()
              section = ccb.get_section(1) or ccb.add_section()
              slot = 1
            end
            if pcall(section.set_slot, slot, { value = { type = t[1], name = name, quality = q }, min = 1 }) then
              slot = slot + 1
            end
          end
        end
      end
    end
    storage.calc = calc
  elseif e.tick == 5 then
    local list = {}
    for _, sv in pairs(storage.calc.get_control_behavior().signals_last_tick or {}) do
      list[#list + 1] = { sv.signal.type or "item", sv.signal.name, sv.signal.quality or "normal" }
    end
    helpers.write_file("fsim/signals.json", helpers.table_to_json(list), false)
  end
end
