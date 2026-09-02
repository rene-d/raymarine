--
-- raymarine_8182.lua — Dissecteur Wireshark pour les messages Raymarine du MFD
--                      exposés en clair sur TCP 8182 (nommage libwp.so).
--
-- Deux services y sont décodés, tous deux little-endian, avec le même en-tête
-- [u32 command][u32 len][u32 appType][u32 msgType] où `len` couvre tout ce qui
-- suit l'octet 8 (trame complète = 8 + len) et la réponse vaut requête + 1 :
--
--   • SSHAccess — enrôlement de la clé publique SSH par RayConnect :
--     0x0016E360 (1500000) SSHAccessRequest  (client → MFD) :
--       [hdr][u32 id_len][id][clé publique jusqu'à la fin]
--     0x0016E361 (1500001) SSHAccessResponse (MFD → client) :
--       [hdr][u32 id_len][id]
--
--   • RequestOwnership — revendication de propriété (clé SSH + certificat) :
--     0x0016E367 (1500007) MessageTypeRequestOwnership (client → MFD) :
--       [hdr][u32 user_len][user][u32 ssh_len][sshKey][u32 cert_len][certKey]
--     0x0016E368 (1500008) réponse — HYPOTHÈSE « requête + 1 », NON observée en
--       capture : seul l'en-tête (cmd/len/appType/msgType) est décodé, le reste
--       est laissé en octets bruts.
--
-- `appType` (=2) sélectionne le service SSHAccess ; `msgType`
-- (SSHAccessResponseMessageType) vaut 0 (None) en requête, et en réponse porte le
-- résultat : 1=KeyAddSuccess, 2=KeyAddFail, 3=AuthRejected, 4=AuthInProgress —
-- RequestOwnership partage ce même énuméré de statut.
--
-- Spécification : « docs/5. protocole-messages-8182.md » (§3 SSHAccess, §6 carte des
-- commandes).
--
-- Installation :
--   Copier dans ~/.local/lib/wireshark/plugins/ (macOS/Linux) puis recharger.
-- Test (depuis un poste où le plugin n'est PAS déjà installé, sinon double-load) :
--   tshark -X lua_script:raymarine_8182.lua -r axiom.pcapng -Y ray8182 -O ray8182
--

local ray = Proto("ray8182", "Raymarine MFD messages (RAY8182, TCP 8182)")

local HDR_LEN = 8                    -- command + len, avant le corps couvert par len

local CMD_SSH_REQ = 0x0016E360       -- 1500000, SSHAccessRequest
local CMD_SSH_RSP = 0x0016E361       -- 1500001, SSHAccessResponse (= requête + 1)
local CMD_OWN_REQ = 0x0016E367       -- 1500007, MessageTypeRequestOwnership
local CMD_OWN_RSP = 0x0016E368       -- 1500008, réponse RequestOwnership (hypothèse 🟡)

local COMMANDS = {
    [CMD_SSH_REQ] = "SSHAccessRequest",
    [CMD_SSH_RSP] = "SSHAccessResponse",
    [CMD_OWN_REQ] = "RequestOwnership",
    [CMD_OWN_RSP] = "RequestOwnershipResponse",
}

-- 4e u32 = SSHAccessResponseMessageType : 0 en requête, résultat en réponse.
local MSGTYPE = {
    [0] = "None", [1] = "KeyAddSuccess", [2] = "KeyAddFail",
    [3] = "AuthRejected", [4] = "AuthInProgress",
}

local f = ray.fields
f.command = ProtoField.uint32("ray8182.command",   "Command", base.DEC, COMMANDS)
f.len     = ProtoField.uint32("ray8182.len",       "Length (rest)", base.DEC)
f.apptype = ProtoField.uint32("ray8182.app_type",  "AppType", base.DEC)
f.msgtype = ProtoField.uint32("ray8182.msg_type",  "MsgType (status)", base.DEC, MSGTYPE)
-- SSHAccess (1500000/1500001)
f.id_len  = ProtoField.uint32("ray8182.id_len",    "Identity length", base.DEC)
f.id      = ProtoField.string("ray8182.identity",  "Identity")
f.pubkey  = ProtoField.string("ray8182.pubkey",    "SSH public key")
-- RequestOwnership (1500007)
f.user_len = ProtoField.uint32("ray8182.user_len", "Username length", base.DEC)
f.user     = ProtoField.string("ray8182.username", "Username")
f.ssh_len  = ProtoField.uint32("ray8182.ssh_len",  "SSH key length", base.DEC)
f.cert_len = ProtoField.uint32("ray8182.cert_len", "Certificate length", base.DEC)
f.cert     = ProtoField.string("ray8182.cert",     "Certificate")
f.rest     = ProtoField.bytes("ray8182.rest",      "Undissected bytes")

local e_status = ProtoExpert.new("ray8182.bad_status",
    "response status != KeyAddSuccess (fail/rejected/in-progress)",
    expert.group.RESPONSE_CODE, expert.severity.WARN)
ray.experts = { e_status }

-- Abrège une clé publique OpenSSH pour la colonne Info (type + longueur du blob).
local function key_summary(key)
    local ktype, blob = key:match("^(%S+)%s+(%S+)")
    if ktype and blob then
        return string.format("%s (%d b64)", ktype, #blob)
    end
    return key:sub(1, 24)
end

-- Lit un champ préfixé par un u32 de longueur (LE) à `off`, borné par `total`.
-- Ajoute la longueur (len_field) et la valeur (val_field) au sous-arbre `item`.
-- Renvoie l'offset suivant et la valeur (string), ou nil si l'en-tête déborde.
local function add_lp(item, len_field, val_field, tvb, off, total)
    if off + 4 > total then return nil end
    local n = tvb(off, 4):le_uint()
    if off + 4 + n > total then n = total - (off + 4) end   -- borne défensive
    item:add_le(len_field, tvb(off, 4))
    local val = ""
    if n > 0 then
        item:add(val_field, tvb(off + 4, n))
        val = tvb(off + 4, n):string()
    end
    return off + 4 + n, val
end

function ray.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len == 0 then return 0 end

    -- en-tête minimal pour lire la commande et la longueur
    if len < HDR_LEN then
        pinfo.desegment_offset = 0
        pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
        return len
    end

    local command = tvb(0, 4):le_uint()
    if not COMMANDS[command] then return 0 end  -- pas nous : laisser la main

    local body = tvb(4, 4):le_uint()
    local total = HDR_LEN + body
    if len < total then                         -- trame à cheval : réassembler
        pinfo.desegment_offset = 0
        pinfo.desegment_len = total - len
        return len
    end

    pinfo.cols.protocol = "RAY8182"
    local msgtype = tvb(12, 4):le_uint()
    local is_response = (command == CMD_SSH_RSP or command == CMD_OWN_RSP)

    -- en-tête commun
    local item = tree:add(ray, tvb(0, total), "RAY8182")
    item:add_le(f.command, tvb(0, 4))
    item:add_le(f.len,     tvb(4, 4))
    item:add_le(f.apptype, tvb(8, 4))
    local mt = item:add_le(f.msgtype, tvb(12, 4))     -- 0 en requête, statut en réponse
    if is_response and msgtype ~= 1 then              -- réponse : != KeyAddSuccess
        mt:add_proto_expert_info(e_status)
    end

    local summary
    if command == CMD_SSH_REQ then
        local id_len = tvb(16, 4):le_uint()
        local ident = tvb(20, id_len):string()
        local key = tvb(20 + id_len, total - (20 + id_len)):string()
        item:add_le(f.id_len, tvb(16, 4))
        item:add(f.id, tvb(20, id_len))
        item:add(f.pubkey, tvb(20 + id_len, total - (20 + id_len)))
        summary = string.format("SSHAccess req %s  %s", ident, key_summary(key))

    elseif command == CMD_SSH_RSP then
        local id_len = tvb(16, 4):le_uint()
        local ident = tvb(20, id_len):string()
        item:add_le(f.id_len, tvb(16, 4))
        item:add(f.id, tvb(20, id_len))
        summary = string.format("SSHAccess rsp %s  %s", ident,
            MSGTYPE[msgtype] or ("msgType=" .. msgtype))

    elseif command == CMD_OWN_REQ then
        -- trois champs préfixés par leur longueur : user, sshKey, certKey
        local off, user = add_lp(item, f.user_len, f.user, tvb, 16, total)
        local key, cert = "", ""
        if off then off, key  = add_lp(item, f.ssh_len,  f.pubkey, tvb, off, total) end
        if off then off, cert = add_lp(item, f.cert_len, f.cert,   tvb, off, total) end
        summary = string.format("Owner req %s  %s  +cert(%d b)",
            user or "?", key_summary(key or ""), #(cert or ""))

    else  -- CMD_OWN_RSP (1500008) : réponse non observée, en-tête seul (🟡)
        if total > 16 then item:add(f.rest, tvb(16, total - 16)) end
        summary = string.format("Owner rsp  %s",
            MSGTYPE[msgtype] or ("msgType=" .. msgtype))
    end

    item:append_text("  " .. summary)
    pinfo.cols.info = summary
    return total
end

DissectorTable.get("tcp.port"):add(8182, ray)
