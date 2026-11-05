-- Current target, when it's something the player can fight: the website shows
-- it as what you're fighting. Other players are sent without their name.

local FT = ForeverTracker
local safe, read = FT.Safe, FT.Read

local RANKS = { elite = true, rare = true, rareelite = true, worldboss = true }

function FT.ReadTarget()
	if not read(nil, UnitExists, "target") then return nil end
	if not read(nil, UnitCanAttack, "player", "target") then return nil end
	local t = {
		level = read(nil, UnitLevel, "target"),   -- -1 for "??" (far above you)
		pct = FT.Percent(nil, UnitHealthPercent, "target", false, CurveConstants and CurveConstants.ScaleTo100),
		dead = read(nil, UnitIsDead, "target") and true or nil,
	}
	local rank = read(nil, UnitClassification, "target")
	if RANKS[rank] then t.rank = rank end
	if read(nil, UnitIsPlayer, "target") then
		t.player = true
		local ok, class = pcall(UnitClass, "target")
		t.class = ok and safe(class) or nil
	else
		t.name = read(nil, UnitName, "target")
		-- Lets the companion read the health from the combat log when it's secret here.
		t.guid = read(nil, UnitGUID, "target")
	end
	return t
end
