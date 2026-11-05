-- Appear offline: friends see you as offline and nothing is shared, but the
-- addon keeps recording. When it's turned off again, a popup asks whether to
-- share what was recorded meanwhile (the UI reloads so the saved file holds
-- it all and the companion uploads it) or keep it private (it's deleted).
-- Until then nothing is shared; the question comes back after a reload.

local FT = ForeverTracker

local function recordedSince(t)
	local n = { events = 0, positions = 0, xp = 0 }
	for key in pairs(n) do
		for _, row in ipairs(FT.db[key] or {}) do
			if row.t and row.t >= t then n[key] = n[key] + 1 end
		end
	end
	return n
end

local function share()
	FT.db.offlineSince = nil
	ReloadUI() -- writes the saved file; the companion uploads it, offline time included
end

local function keepPrivate()
	local since = FT.db.offlineSince
	if not since then return end
	for _, key in ipairs({ "events", "positions", "xp" }) do
		local kept = {}
		for _, row in ipairs(FT.db[key] or {}) do
			if not (row.t and row.t >= since) then kept[#kept + 1] = row end
		end
		FT.db[key] = kept
	end
	FT.DropQueuedEvents(since)
	FT.db.offlineSince = nil
	FT.Print("kept private: what was recorded while you appeared offline is deleted")
	FT.Refresh()
end

StaticPopupDialogs = StaticPopupDialogs or {}
StaticPopupDialogs.FOREVERTRACKER_SHARE_OFFLINE = {
	text = "While you appeared offline, Forever Tracker recorded %s.\n\nShare it on the website?",
	button1 = "Share (reloads UI)",
	button2 = "Keep private",
	OnAccept = share,
	-- Only an actual click deletes anything; the popup can't be dismissed otherwise.
	OnCancel = function(_, _, reason) if reason == "clicked" then keepPrivate() end end,
	timeout = 0,
	whileDead = true,
	hideOnEscape = false,
	preferredIndex = 3,
}

function FT.AskAboutOfflineTime()
	local db = FT.db
	if not (db and db.offlineSince) or db.settings.appearOffline then return end
	local n = recordedSince(db.offlineSince)
	if n.events + n.positions + n.xp == 0 then
		db.offlineSince = nil -- nothing happened meanwhile: just come back online
		FT.Refresh()
		return
	end
	local parts = {}
	if n.events > 0 then parts[#parts + 1] = n.events .. (n.events == 1 and " journal entry" or " journal entries") end
	if n.positions > 0 then parts[#parts + 1] = n.positions .. (n.positions == 1 and " map position" or " map positions") end
	if n.xp > 0 then parts[#parts + 1] = n.xp .. (n.xp == 1 and " XP sample" or " XP samples") end
	StaticPopup_Show("FOREVERTRACKER_SHARE_OFFLINE", table.concat(parts, ", "))
end

FT.OnSettingsChanged(function()
	local s = FT.db.settings
	if s.appearOffline then
		FT.db.offlineSince = FT.db.offlineSince or FT.Now()
	else
		FT.AskAboutOfflineTime()
	end
end)

-- An unanswered question (e.g. reloaded before choosing) comes back after login.
FT.OnLoaded(function()
	C_Timer.After(5, FT.AskAboutOfflineTime)
end)
