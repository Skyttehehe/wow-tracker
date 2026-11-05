-- Minimap bubble: shows whether tracking is on, and opens a small box to
-- enter the code that lets the companion upload this character to the site.
-- The code is kept in ForeverTrackerDB.settings.key; the game only writes it
-- to disk on a reload, so saving reloads the UI once.

local FT = ForeverTracker

-- Code box ------------------------------------------------------------------

local dialog

local function commit()
	local code = (dialog.box:GetText() or ""):gsub("%s", "")
	if code == "" then return end
	FT.db.settings.key = code
	ReloadUI()
end

local function buildDialog()
	dialog = CreateFrame("Frame", "ForeverTrackerCodeDialog", UIParent, "BasicFrameTemplateWithInset")
	dialog:SetSize(330, 150)
	dialog:SetPoint("CENTER")
	dialog:SetFrameStrata("DIALOG")
	dialog:EnableMouse(true)
	dialog:SetMovable(true)
	dialog:RegisterForDrag("LeftButton")
	dialog:SetScript("OnDragStart", dialog.StartMoving)
	dialog:SetScript("OnDragStop", dialog.StopMovingOrSizing)
	tinsert(UISpecialFrames, "ForeverTrackerCodeDialog") -- Escape closes it

	local title = dialog:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
	title:SetPoint("TOP", 0, -5)
	title:SetText("Forever Tracker")

	local text = dialog:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	text:SetPoint("TOPLEFT", 16, -34)
	text:SetPoint("RIGHT", -16, 0)
	text:SetJustifyH("LEFT")
	text:SetText("Paste the code you were given. The game reloads once to save it.")

	local box = CreateFrame("EditBox", nil, dialog, "InputBoxTemplate")
	box:SetSize(280, 22)
	box:SetPoint("TOPLEFT", text, "BOTTOMLEFT", 6, -12)
	box:SetAutoFocus(true)
	box:SetScript("OnEnterPressed", commit)
	box:SetScript("OnEscapePressed", function() dialog:Hide() end)
	dialog.box = box

	local save = CreateFrame("Button", nil, dialog, "UIPanelButtonTemplate")
	save:SetSize(130, 22)
	save:SetPoint("BOTTOMRIGHT", -14, 12)
	save:SetText("Save and reload")
	save:SetScript("OnClick", commit)
end

function FT.ShowCodeDialog()
	if not dialog then buildDialog() end
	dialog.box:SetText(FT.db.settings.key or "")
	dialog:Show()
end

-- Minimap bubble -----------------------------------------------------------

local button

local function place()
	local angle = math.rad(FT.db.settings.minimapAngle or 200)
	local radius = Minimap:GetWidth() / 2 + 5
	button:ClearAllPoints()
	button:SetPoint("CENTER", Minimap, "CENTER", math.cos(angle) * radius, math.sin(angle) * radius)
end

local function ready()
	local s = FT.db.settings
	return s.enabled and not s.appearOffline and s.key ~= nil
end

local function update()
	if not button then return end
	button:SetShown(not FT.db.settings.minimapHidden)
	button.icon:SetDesaturated(not ready())
end

local function tooltip(self)
	local s = FT.db.settings
	GameTooltip:SetOwner(self, "ANCHOR_LEFT")
	GameTooltip:AddLine("Forever Tracker")
	if not s.enabled then
		GameTooltip:AddLine("Tracking is off", 1, 0.4, 0.4)
	elseif s.appearOffline then
		GameTooltip:AddLine("Appearing offline (still recording privately)", 1, 0.7, 0.3)
	elseif FT.db.offlineSince then
		GameTooltip:AddLine("Offline until you answer: share your offline time?", 1, 0.7, 0.3)
	else
		GameTooltip:AddLine("Sharing live", 0.4, 1, 0.4)
	end
	if s.key then
		GameTooltip:AddLine("Code entered", 0.4, 1, 0.4)
	else
		GameTooltip:AddLine("No code yet: left-click to enter it", 1, 0.82, 0)
	end
	GameTooltip:AddLine(" ")
	GameTooltip:AddLine("Left-click: enter your code", 0.8, 0.8, 0.8)
	GameTooltip:AddLine("Right-click: options", 0.8, 0.8, 0.8)
	GameTooltip:AddLine("Drag: move around the minimap", 0.8, 0.8, 0.8)
	GameTooltip:Show()
end

local function create()
	if button or not Minimap then return end
	button = CreateFrame("Button", "ForeverTrackerMinimapButton", Minimap)
	button:SetSize(31, 31)
	button:SetFrameStrata("MEDIUM")
	button:SetFrameLevel(8)
	button:SetHighlightTexture("Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight")

	local bg = button:CreateTexture(nil, "BACKGROUND")
	bg:SetTexture("Interface\\Minimap\\UI-Minimap-Background")
	bg:SetSize(20, 20)
	bg:SetPoint("TOPLEFT", 7, -5)
	local icon = button:CreateTexture(nil, "ARTWORK")
	icon:SetTexture("Interface\\Icons\\INV_Misc_Map_01")
	icon:SetSize(18, 18)
	icon:SetPoint("TOPLEFT", 7, -6)
	button.icon = icon
	local border = button:CreateTexture(nil, "OVERLAY")
	border:SetTexture("Interface\\Minimap\\MiniMap-TrackingBorder")
	border:SetSize(53, 53)
	border:SetPoint("TOPLEFT")

	button:RegisterForClicks("LeftButtonUp", "RightButtonUp")
	button:RegisterForDrag("LeftButton")
	button:SetScript("OnClick", function(_, which)
		if which == "RightButton" then
			ForeverTracker_ToggleOptions()
		else
			FT.ShowCodeDialog()
		end
	end)
	button:SetScript("OnDragStart", function(self)
		self:SetScript("OnUpdate", function()
			local mx, my = Minimap:GetCenter()
			local cx, cy = GetCursorPosition()
			local scale = Minimap:GetEffectiveScale()
			FT.db.settings.minimapAngle = math.deg(math.atan2(cy / scale - my, cx / scale - mx))
			place()
		end)
	end)
	button:SetScript("OnDragStop", function(self) self:SetScript("OnUpdate", nil) end)
	button:SetScript("OnEnter", tooltip)
	button:SetScript("OnLeave", GameTooltip_Hide)

	place()
	update()
end

FT.OnLoaded(create)
FT.OnSettingsChanged(update)
