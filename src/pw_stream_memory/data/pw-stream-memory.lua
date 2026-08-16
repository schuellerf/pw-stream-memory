-- SPDX-License-Identifier: MIT
-- Apply pw-stream-memory sidecar after native stream restore.
-- Matches application.process.binary only (Electron apps such as Slack).
--
-- WirePlumber's Lua sandbox has no io.* / os.execute. Sidecar JSON is mirrored
-- into a Wp.State file by the TUI. The live "loaded" marker is a PipeWire
-- metadata key on the default metadata object.

log = Log.open_topic ("pw-stream-memory")

BINARY_PROP = "application.process.binary"
HOOK_META_KEY = "pw-stream-memory.hook"
SIDECAR_STATE_NAME = "pw-stream-memory"

function media_class_key (properties)
  local mc = properties ["media.class"] or "Stream/Output/Audio"
  return mc:gsub ("^Stream/", "")
end

function as_table (value)
  if type (value) == "table" then
    return value
  end
  if value ~= nil and type (value.parse) == "function" then
    local ok, parsed = pcall (function () return value:parse () end)
    if ok and type (parsed) == "table" then
      return parsed
    end
  end
  return nil
end

function override_list (items)
  local tbl = as_table (items)
  if not tbl then
    return {}
  end
  if tbl [1] then
    return tbl
  end
  local out = {}
  for _, ov in pairs (tbl) do
    if type (ov) == "table" then
      table.insert (out, ov)
    end
  end
  return out
end

function load_overrides ()
  local st = State (SIDECAR_STATE_NAME)
  local props = st:load ()
  local raw = props and props ["overrides"]
  if type (raw) ~= "string" or raw == "" then
    return {}
  end
  local json = Json.Raw (raw)
  if not json or not json:is_object () then
    log:warning ("invalid sidecar JSON in Wp.State " .. SIDECAR_STATE_NAME)
    return {}
  end
  local ok, parsed = pcall (function () return json:parse () end)
  if not ok or type (parsed) ~= "table" then
    log:warning ("could not parse sidecar JSON")
    return {}
  end
  return override_list (parsed.overrides)
end

function override_matches (ov, properties)
  if type (ov) ~= "table" then
    return false
  end
  if ov.prop ~= BINARY_PROP then
    return false
  end
  local prop = ov.prop
  local value = ov.value
  if type (prop) ~= "string" or type (value) ~= "string" or prop == "" then
    return false
  end
  if properties [prop] ~= value then
    return false
  end
  local want = ov.media_class
  if type (want) == "string" and want ~= "" then
    local have = media_class_key (properties)
    local raw = properties ["media.class"] or ""
    if want ~= have and want ~= raw then
      return false
    end
  end
  return true
end

function find_override (properties)
  for _, ov in ipairs (load_overrides ()) do
    if override_matches (ov, properties) then
      return ov
    end
  end
  return nil
end

function copy_array (src)
  local tbl = as_table (src)
  if not tbl then
    return nil
  end
  local out = {}
  for _, v in ipairs (tbl) do
    table.insert (out, v)
  end
  if #out == 0 then
    return nil
  end
  return out
end

function apply_override (event, node, ov)
  local stream_props = node.properties
  local props = {
    "Spa:Pod:Object:Param:Props", "Props",
    volume = ov.volume,
    mute = ov.mute,
  }
  local vols = copy_array (ov.channelVolumes)
  if vols then
    table.insert (vols, 1, "Spa:Float")
    props.channelVolumes = Pod.Array (vols)
  end
  local cmap = copy_array (ov.channelMap)
  if cmap then
    local arr = { "Spa:Enum:AudioChannel" }
    for _, v in ipairs (cmap) do
      table.insert (arr, v)
    end
    props.channelMap = Pod.Array (arr)
  end

  if props.volume or (props.mute ~= nil) or props.channelVolumes or props.channelMap then
    local param = Pod.Object (props)
    log:info (node, "lua sidecar restore " .. tostring (ov.prop) .. "=" .. tostring (ov.value))
    node:set_param ("Props", param)
  end

  local target = ov.target
  if type (target) == "string" and target ~= "" then
    local target_in_props =
        stream_props ["target.object"] or stream_props ["node.target"]
    if not target_in_props then
      local source = event:get_source ()
      local nodes_om = source:call ("get-object-manager", "node")
      local metadata_om = source:call ("get-object-manager", "metadata")
      local target_node = nodes_om:lookup {
        Constraint { "node.name", "=", target, type = "pw" }
      }
      local metadata = metadata_om:lookup {
        Constraint { "metadata.name", "=", "default" }
      }
      if target_node and metadata then
        metadata:set (node ["bound-id"], "target.object", "Spa:Id",
            target_node.properties ["object.serial"])
      end
    end
  end
end

function default_metadata ()
  local plugin = Plugin.find ("standard-event-source")
  if not plugin then
    return nil
  end
  local om = plugin:call ("get-object-manager", "metadata")
  if not om then
    return nil
  end
  return om:lookup {
    Constraint { "metadata.name", "=", "default" }
  }
end

function set_live_marker ()
  local ok, result = pcall (function ()
    local metadata = default_metadata ()
    if not metadata then
      return false
    end
    metadata:set (0, HOOK_META_KEY, "Spa:String", "loaded")
    log:info ("live hook marker set on default metadata")
    return true
  end)
  if not ok then
    log:warning ("failed to set live hook marker: " .. tostring (result))
    return false
  end
  return result and true or false
end

-- This script is loaded after support.standard-event-source, so metadata-added
-- has usually already fired. Set immediately, then retry a few times.
marker_attempts = 0
function ensure_live_marker ()
  if set_live_marker () then
    return false
  end
  marker_attempts = marker_attempts + 1
  if marker_attempts >= 25 then
    log:warning ("could not set live hook marker on default metadata")
    return false
  end
  return true
end

if not set_live_marker () then
  Core.timeout_add (200, ensure_live_marker)
end

marker_hook = SimpleEventHook {
  name = "pw-stream-memory/mark-loaded",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "metadata-added" },
      Constraint { "metadata.name", "=", "default" },
    },
  },
  execute = function (event)
    set_live_marker ()
  end
}
marker_hook:register ()

apply_hook = SimpleEventHook {
  name = "pw-stream-memory/apply-binary-override",
  after = { "node/restore-stream" },
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "node-added" },
      Constraint { "media.class", "matches", "Stream/*" },
    },
  },
  execute = function (event)
    local ok, err = pcall (function ()
      local node = event:get_subject ()
      local ov = find_override (node.properties)
      if not ov then
        return
      end
      apply_override (event, node, ov)
    end)
    if not ok then
      log:warning ("apply-binary-override failed: " .. tostring (err))
    end
  end
}

apply_hook:register ()
