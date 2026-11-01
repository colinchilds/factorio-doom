-- Discovery script: build one of each entity we simulate, configure every option through the
-- Lua API, then export a blueprint so we learn the exact 2.1 blueprint JSON field names.

local function surface()
  local s = game.surfaces["fsim"]
  if s then return s end
  s = game.create_surface("fsim", {
    default_enable_all_autoplace_controls = false,
    autoplace_settings = {
      entity = { treat_missing_as_default = false, settings = {} },
      decorative = { treat_missing_as_default = false, settings = {} },
    },
    no_enemies_mode = true,
  })
  s.request_to_generate_chunks({ 0, 0 }, 2)
  s.force_generate_chunk_requests()
  local tiles = {}
  for x = -32, 31 do
    for y = -32, 31 do tiles[#tiles + 1] = { name = "lab-dark-1", position = { x, y } } end
  end
  s.set_tiles(tiles)
  return s
end

local function sig(name, typ) return { type = typ or "virtual", name = name } end

return function(e)
  if e.tick ~= 1 then return end
  local s = surface()
  local f = game.forces.player

  local dec = s.create_entity { name = "decider-combinator", position = { 0.5, 1 }, force = f, direction = defines.direction.north }
  local cb = dec.get_or_create_control_behavior()
  cb.parameters = {
    conditions = {
      { first_signal = sig("signal-each"), first_signal_networks = { red = true, green = false }, comparator = ">",
        second_signal = sig("signal-each"), second_signal_networks = { red = false, green = true } },
      { first_signal = sig("signal-A"), constant = 5, comparator = "=", compare_type = "and" },
    },
    outputs = { { signal = sig("signal-each"), copy_count_from_input = true, networks = { red = true, green = false } } },
    else_outputs = { { signal = sig("signal-each"), copy_count_from_input = false, constant = 7 } },
  }
  cb.input_networks = { red = true, green = false }
  cb.output_networks = { red = false, green = true }

  local ari = s.create_entity { name = "arithmetic-combinator", position = { 2.5, 1 }, force = f }
  local acb = ari.get_or_create_control_behavior()
  acb.parameters = { first_signal = sig("signal-each"), first_signal_networks = { red = true, green = false },
    operation = ">>", second_signal = sig("signal-each"), second_signal_networks = { red = false, green = true },
    output_signal = sig("signal-each") }

  local sel = s.create_entity { name = "selector-combinator", position = { 4.5, 1 }, force = f }
  sel.get_or_create_control_behavior().parameters = { operation = "select", select_max = false, index_constant = 2, index_comparator = "<" }
  local sel2 = s.create_entity { name = "selector-combinator", position = { 6.5, 1 }, force = f }
  sel2.get_or_create_control_behavior().parameters = { operation = "time", game_tick_signal = sig("signal-T") }

  local cc = s.create_entity { name = "constant-combinator", position = { 8.5, 0.5 }, force = f }
  local sec = cc.get_or_create_control_behavior().get_section(1)
  sec.set_slot(1, { value = { type = "virtual", name = "signal-A", quality = "normal" }, min = 42 })
  sec.set_slot(2, { value = { type = "item", name = "iron-plate", quality = "normal" }, min = -3 })

  local lamp = s.create_entity { name = "small-lamp", position = { 10.5, 0.5 }, force = f }
  lamp.always_on = true
  local lcb = lamp.get_or_create_control_behavior()
  lcb.use_colors = true
  lcb.color_mode = defines.control_behavior.lamp.color_mode.packed_rgb
  lcb.rgb_signal = sig("signal-R")

  local pole = s.create_entity { name = "medium-electric-pole", position = { 12.5, 0.5 }, force = f }
  local panel = s.create_entity { name = "display-panel", position = { 14.5, 0.5 }, force = f }

  local W = defines.wire_connector_id
  dec.get_wire_connector(W.combinator_output_red, true).connect_to(ari.get_wire_connector(W.combinator_input_red, true))
  ari.get_wire_connector(W.combinator_output_green, true).connect_to(sel.get_wire_connector(W.combinator_input_green, true))
  cc.get_wire_connector(W.circuit_red, true).connect_to(lamp.get_wire_connector(W.circuit_red, true))
  lamp.get_wire_connector(W.circuit_green, true).connect_to(pole.get_wire_connector(W.circuit_green, true))

  local inv = game.create_inventory(1)
  inv[1].set_stack("blueprint")
  inv[1].create_blueprint { surface = s, force = f, area = { { -1, -1 }, { 17, 3 } } }
  helpers.write_file("fsim/discover.txt", inv[1].export_stack(), false)
  local ok, err = pcall(function() return dec.get_control_behavior().input_networks end)
  helpers.write_file("fsim/discover-log.txt", serpent.block({ in_nets = ok and err or tostring(err) }), false)
end
