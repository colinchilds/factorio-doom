-- Pole survey: supply area and wire reach of each electric pole at every quality. run.py --poles
-- runs this with the DLC mods on (quality levels above normal need the quality mod).

return function(e)
  if e.tick ~= 1 then return end
  local out = {}
  for _, pole in ipairs({ "substation", "medium-electric-pole", "small-electric-pole", "big-electric-pole" }) do
    local p = prototypes.entity[pole]
    out[pole] = {}
    for qname, q in pairs(prototypes.quality) do
      if not q.hidden then
        out[pole][qname] = { level = q.level, supply_area_distance = p.get_supply_area_distance(q),
                             max_wire_distance = p.get_max_wire_distance(q),
                             max_circuit_wire_distance = p.get_max_circuit_wire_distance(q) }
      end
    end
  end
  helpers.write_file("fsim/poles.json", helpers.table_to_json(out), false)
end
