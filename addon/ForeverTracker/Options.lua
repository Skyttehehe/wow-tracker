-- In-game settings: a standalone window (/ft) plus the same controls in the
-- game's AddOns options panel.

local FT = ForeverTracker
local views = {}

local function label(parent, text, template)
	local fs = parent:CreateFontString(nil, "ARTWORK", template or "GameFontHighlight")
	fs:SetText(text)
	fs:SetJustifyH("LEFT")
	return fs
end

local function checkbox(parent, text, tooltip, onClick)
	local cb = CreateFrame("CheckButton", nil, parent, "UICheckButtonTemplate")
	cb:SetSize(26, 26)
	cb.label = label(parent, text)
	cb.label:SetPoint("LEFT", cb, "RIGHT", 4, 1)
	cb:SetScript("OnClick", function(self) onClick(self:GetChecked() and true or false) end)
	cb:SetScript("OnEnter", function(self)
		GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
		GameTooltip:SetText(text, 1, 1, 1)
		GameTooltip:AddLine(tooltip, nil, nil, nil, true)
		GameTooltip:Show()
	end)
	cb:SetScript("OnLeave", GameTooltip_Hide)
	return cb
end

local function button(parent, text, width, onClick)
	local b = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
	b:SetSize(width, 22)
	b:SetText(text)
	b:SetScript("OnClick", onClick)
	return b
end

-- Builds the controls into `parent`, anchored below `top`, and returns a
-- refresh function.
local function build(parent, top)
	local enabled = checkbox(parent, "Enable tracking",
		"When off, nothing is recorded and the companion app sends nothing.",
		function(v) FT.SetSetting("enabled", v) end)
	enabled:SetPoint("TOPLEFT", top, "BOTTOMLEFT", 0, -12)

	local hidden = checkbox(parent, "Appear offline",
		"Your friends see you as offline. Nothing is recorded while this is on, so it can't show up later.",
		function(v) FT.SetSetting("appearOffline", v) end)
	hidden:SetPoint("TOPLEFT", enabled, "BOTTOMLEFT", 0, -4)

	local combatLog = checkbox(parent, "Share health from the combat log",
		"Turns on the game's combat log file (with advanced logging). The game writes your current health "
		.. "and mana there during combat, and the companion reads it. The file grows while you play.",
		function(v) FT.SetSetting("combatLog", v) end)
	combatLog:SetPoint("TOPLEFT", hidden, "BOTTOMLEFT", 0, -4)

	local epLabel = label(parent, "Backend address (LAN IP:port)", "GameFontNormal")
	epLabel:SetPoint("TOPLEFT", combatLog, "BOTTOMLEFT", 4, -14)

	local ep = CreateFrame("EditBox", nil, parent, "InputBoxTemplate")
	ep:SetSize(170, 22)
	ep:SetAutoFocus(false)
	ep:SetMaxLetters(32)
	ep:SetPoint("TOPLEFT", epLabel, "BOTTOMLEFT", 6, -4)

	local epError = label(parent, "", "GameFontRedSmall")
	epError:SetPoint("TOPLEFT", ep, "BOTTOMLEFT", -6, -4)

	local function saveEndpoint()
		if FT.SetEndpoint(ep:GetText()) then
			epError:SetText("")
			ep:ClearFocus()
		else
			epError:SetText("Must be a private IPv4 address, e.g. 192.168.1.221:8787")
		end
	end
	ep:SetScript("OnEnterPressed", saveEndpoint)
	ep:SetScript("OnEscapePressed", function(self)
		self:SetText(FT.db.settings.endpoint)
		epError:SetText("")
		self:ClearFocus()
	end)

	local save = button(parent, "Save", 60, saveEndpoint)
	save:SetPoint("LEFT", ep, "RIGHT", 6, 0)

	local status = label(parent, "")
	status:SetPoint("TOPLEFT", epError, "BOTTOMLEFT", 0, -10)

	local sync = button(parent, "Sync now", 100, ForeverTracker_Sync)
	sync:SetPoint("TOPLEFT", status, "BOTTOMLEFT", 0, -10)

	local help = label(parent,
		"Live data goes out through the small strip in the top-left corner of "
		.. "your screen. The full history (trail, journal) is saved on reload, "
		.. "logout or exit; Sync now reloads your UI to save it immediately.",
		"GameFontDisableSmall")
	help:SetPoint("TOPLEFT", sync, "BOTTOMLEFT", 0, -10)
	help:SetWidth(300)

	return function()
		local s = FT.db.settings
		enabled:SetChecked(s.enabled)
		hidden:SetChecked(s.appearOffline)
		hidden:SetEnabled(s.enabled)
		combatLog:SetChecked(s.combatLog)
		combatLog:SetEnabled(s.enabled)
		if not ep:HasFocus() then ep:SetText(s.endpoint) end
		status:SetText("Status: " .. FT.StatusText())
	end
end

local window
local function createWindow()
	window = CreateFrame("Frame", "ForeverTrackerOptions", UIParent, "BasicFrameTemplateWithInset")
	window:SetSize(340, 330)
	window:SetPoint("CENTER")
	window:SetMovable(true)
	window:EnableMouse(true)
	window:RegisterForDrag("LeftButton")
	window:SetScript("OnDragStart", window.StartMoving)
	window:SetScript("OnDragStop", window.StopMovingOrSizing)
	window:SetFrameStrata("DIALOG")
	tinsert(UISpecialFrames, "ForeverTrackerOptions") -- close on Escape

	local title = label(window, "Forever Tracker", "GameFontNormal")
	title:SetPoint("TOP", 0, -5)

	local anchor = CreateFrame("Frame", nil, window)
	anchor:SetSize(1, 1)
	anchor:SetPoint("TOPLEFT", 14, -26)

	local refresh = build(window, anchor)
	views[#views + 1] = refresh
	window:SetScript("OnShow", refresh)
	window:Hide()
end

function ForeverTracker_ToggleOptions()
	if not window then createWindow() end
	window:SetShown(not window:IsShown())
end

local function registerPanel()
	local panel = CreateFrame("Frame")
	panel.name = "Forever Tracker"

	local title = label(panel, "Forever Tracker", "GameFontNormalLarge")
	title:SetPoint("TOPLEFT", 16, -16)

	local refresh = build(panel, title)
	views[#views + 1] = refresh
	panel:SetScript("OnShow", refresh)

	if Settings and Settings.RegisterCanvasLayoutCategory then
		local category = Settings.RegisterCanvasLayoutCategory(panel, panel.name)
		Settings.RegisterAddOnCategory(category)
	elseif InterfaceOptions_AddCategory then
		InterfaceOptions_AddCategory(panel)
	end
end

FT.OnSettingsChanged(function()
	for _, refresh in ipairs(views) do refresh() end
end)

FT.OnLoaded(registerPanel)
