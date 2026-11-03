-- doom-input: the shortcut button and the inputs it listens to (control.lua has the details).
local P = "__doom-input__/graphics/"
local inputs = {
  {
    type = "shortcut",
    name = "doom-input-toggle",
    order = "z[doom-input]",
    action = "lua",
    toggleable = true,
    associated_control_input = "doom-input-toggle",
    icon = P .. "joystick-x56.png",
    icon_size = 56,
    small_icon = P .. "joystick-x24.png",
    small_icon_size = 24,
  },
  { type = "custom-input", name = "doom-input-toggle", key_sequence = "CONTROL + SHIFT + J", consuming = "none", action = "lua" },
  -- "Use" has no game control of its own; rebind it under Settings > Controls > Mods.
  { type = "custom-input", name = "doom-input-use", key_sequence = "F", consuming = "none", action = "lua" },
}
-- Links to the game's own controls, whatever they're bound to: a press shows up here the tick it
-- happens, before walking_state / shooting_state follow it.
for _, c in ipairs({ "move-up", "move-down", "move-left", "move-right", "shoot-enemy", "next-weapon" }) do
  inputs[#inputs + 1] = { type = "custom-input", name = "doom-input-" .. c, key_sequence = "", linked_game_control = c, consuming = "none", action = "lua" }
end
data:extend(inputs)
