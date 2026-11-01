-- Lane-signal survey: every item/fluid/entity/recipe/virtual signal with the flags that make it
-- unsafe in a blueprint meant for other people's games (hidden, parameter). run.py --lanes runs
-- this with and without the DLC mods and keeps the signals that are safe in both.

local function flag(p, key)
  local ok, v = pcall(function() return p[key] end)
  return ok and v == true
end

return function(e)
  if e.tick ~= 1 then return end
  local list = {}
  for _, t in ipairs({ { "item", prototypes.item }, { "fluid", prototypes.fluid }, { "entity", prototypes.entity },
                       { "recipe", prototypes.recipe }, { "virtual", prototypes.virtual_signal } }) do
    for name, p in pairs(t[2]) do
      list[#list + 1] = { type = t[1], name = name, hidden = flag(p, "hidden"), parameter = flag(p, "parameter"),
                          special = flag(p, "special") }
    end
  end
  helpers.write_file("fsim/lanes.json", helpers.table_to_json(list), false)
end
