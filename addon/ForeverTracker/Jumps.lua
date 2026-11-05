-- Jump counter. The game has no jump event, but the jump key runs
-- JumpOrAscendStart, which addons may watch (not change). Presses while
-- swimming or flying (ascending), or again mid-air, aren't jumps.
-- Kept per character in ForeverTrackerDB.jumps["Name-Realm"].

local FT = ForeverTracker
local read = FT.Read

local AIRTIME = 0.6 -- seconds; a second press within this is mid-air
local lastJump = 0
FT.jumpsVersion = 0

local function characterKey()
	local name, realm = read(nil, UnitName, "player"), read(nil, GetRealmName)
	return name and (name .. "-" .. (realm or "")) or nil
end

function FT.Jumps()
	local key = FT.db and characterKey()
	return key and FT.db.jumps and FT.db.jumps[key] or 0
end

local function onJump()
	if not (FT.db and FT.IsRecording()) then return end
	if read(nil, IsSwimming) or read(nil, IsFlying) then return end
	local now = GetTime()
	if now - lastJump < AIRTIME then return end
	lastJump = now
	local key = characterKey()
	if not key then return end
	FT.db.jumps = FT.db.jumps or {}
	FT.db.jumps[key] = (FT.db.jumps[key] or 0) + 1
	FT.jumpsVersion = FT.jumpsVersion + 1
end

if JumpOrAscendStart then
	hooksecurefunc("JumpOrAscendStart", onJump)
end
