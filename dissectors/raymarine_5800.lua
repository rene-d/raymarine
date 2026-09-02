--
-- raymarine_5800.lua — Dissecteur Wireshark pour ray5800, le protocole d'annonce
--                      Raymarine (multicast UDP 5800). SeaTalk-HS est le nom du
--                      RÉSEAU physique, pas celui de ce protocole : faute de nom
--                      officiel, on l'appelle ici « ray5800 ».
--
-- Décode :
--   * les annonces d'équipement (type 1 / 2) : u1, handle, descriptor, IP, nom,
--     longueur de queue et, pour le type 2, l'extension de 14 octets ;
--   * les annonces de service (type 0) : en-tête fixe de 20 octets puis une
--     payload faite d'endpoints IP:PORT (8 o = 1 endpoint, 16 o = 2 endpoints).
--
-- Spécification : voir docs/1. protocole-udp5800.md
--
-- Installation :
--   Copier ce fichier dans le dossier des plugins Lua personnels de Wireshark :
--     macOS  : ~/.local/lib/wireshark/plugins/   (ou ~/.config/wireshark/plugins/)
--     Linux  : ~/.local/lib/wireshark/plugins/
--     Windows: %APPDATA%\Wireshark\plugins\
--   puis relancer Wireshark (ou Analyze > Reload Lua Plugins).
--
-- Test en ligne de commande (le plugin étant déjà installé, ne PAS ajouter
-- -X lua_script: — le protocole serait enregistré deux fois et tshark refuse) :
--   tshark -r capture.pcapng -Y ray5800
--

local ray = Proto("ray5800", "Raymarine Discovery ray5800 (UDP 5800)")

-- ------------------------------------------------------------------ champs --
local MSG_TYPES = { [0] = "Service announce (channel)",
                    [1] = "Device announce (compact)",
                    [2] = "Device announce (extended)" }

local f = ray.fields
f.type        = ProtoField.uint32("ray5800.type", "Type", base.DEC, MSG_TYPES)
f.u1          = ProtoField.uint32("ray5800.u1", "u1 @4 (unresolved)", base.HEX)
f.handle      = ProtoField.bytes ("ray5800.handle", "Device handle (unique)")
f.descriptor  = ProtoField.uint32("ray5800.descriptor", "Descriptor (type/model)", base.HEX)
f.dev_subtype = ProtoField.uint8 ("ray5800.dev_subtype", "Device subtype (descriptor byte 0)", base.HEX)
f.class_word  = ProtoField.uint16("ray5800.class_word", "Class word (0x840b=node, 0x0000=radar)", base.HEX)
f.ip          = ProtoField.string("ray5800.ip", "IP (little-endian)")
f.name        = ProtoField.string("ray5800.name", "Device name (ASCIIZ)")
f.trail_len   = ProtoField.uint16("ray5800.trail_len", "Trailing length (bytes from @54)", base.DEC)
f.flag        = ProtoField.uint8 ("ray5800.flag", "Flag @54", base.DEC)
f.ext         = ProtoField.bytes ("ray5800.ext", "Type-2 extension (14 bytes @56)")
f.ext_id      = ProtoField.uint32("ray5800.ext_id", "Ext id (unexplained)", base.HEX)
f.ext_variant = ProtoField.uint8 ("ray5800.ext_variant", "Ext variant (0x08=node, 0x02=radar)", base.HEX)
f.raynet_ip   = ProtoField.string("ray5800.raynet_ip", "RayNet IP (198.18.0.0/21)")
f.ext_port    = ProtoField.uint16("ray5800.ext_port", "Ext port (radar: port of its channel record)", base.DEC)

-- Message de type 0 (annonce de service) : en-tête fixe de 20 octets, puis une
-- payload faite d'endpoints IP:PORT (§5 de la spec).
f.rtype       = ProtoField.uint32("ray5800.rtype", "Record type", base.HEX)
f.flags1      = ProtoField.bytes ("ray5800.flags1", "Flags1 @12")
f.flags1_a    = ProtoField.uint16("ray5800.flags1_a", "Flags1 a", base.DEC)
f.flags1_b    = ProtoField.uint16("ray5800.flags1_b", "Flags1 b (30=instrument, 100=radar)", base.DEC)
f.rec_port    = ProtoField.uint16("ray5800.rec_port", "Record port @16 (unicast endpoint port on nodes)", base.DEC)
f.endian      = ProtoField.string("ray5800.endian", "Payload byte order")
f.length      = ProtoField.uint16("ray5800.length", "Payload length", base.DEC)
f.value       = ProtoField.bytes ("ray5800.value", "Payload")
f.endpoint    = ProtoField.string("ray5800.endpoint", "Endpoint (IP:PORT)")
f.ep_ip       = ProtoField.string("ray5800.ep_ip", "Endpoint IP")
f.ep_port     = ProtoField.uint16("ray5800.ep_port", "Endpoint port", base.DEC)
f.ep_extra    = ProtoField.uint16("ray5800.ep_extra", "Endpoint extra (0 on nodes, variable on radar)", base.HEX)
f.unknown     = ProtoField.uint32("ray5800.unknown", "Unknown", base.HEX)

-- Formes de payload, indexées par la longueur annoncée (§5.1) :
--    8 o : [IP:PORT]                       (rtype 07 0f 13)
--   16 o : [IP:PORT][IP:PORT]              (rtype 08 09 1e 23 28 29 2a)
--   20 o : [12 o non résolus][IP:PORT]     (rtype 10)
local EP_OFFSETS = { [8] = { 0 }, [16] = { 0, 8 }, [20] = { 12 } }

-- expert infos
local e_len   = ProtoExpert.new("ray5800.len_mismatch",
                                "Declared length does not match remaining bytes", expert.group.MALFORMED, expert.severity.WARN)
local e_short = ProtoExpert.new("ray5800.too_short",
                                "Payload too short for message type", expert.group.MALFORMED, expert.severity.WARN)
ray.experts = { e_len, e_short }

-- ------------------------------------------------------------- utilitaires --
-- IP little-endian : octets inversés
local function ip_le(rng)
    local b = rng:bytes()
    return string.format("%d.%d.%d.%d",
        b:get_index(3), b:get_index(2), b:get_index(1), b:get_index(0))
end

local function ip_be(rng)
    local b = rng:bytes()
    return string.format("%d.%d.%d.%d",
        b:get_index(0), b:get_index(1), b:get_index(2), b:get_index(3))
end

-- Domaine d'adressage d'un mot de 4 octets, une fois l'ordre des octets connu.
--   198.18.0.0/21 : backbone RayNet (marqueur 12 c6 / c6 12)
--   226.192.0.0/16, 232.0.0.0/8 : groupes multicast (marqueur c0 e2 / e2 c0)
local function domain_of(ip)
    local a, b = ip:match("^(%d+)%.(%d+)%.")
    a, b = tonumber(a), tonumber(b)
    if a == 198 and b == 18 then return "RayNet" end
    if a >= 224 and a <= 239 then return "multicast group" end
    return nil
end

-- Ordre des octets de la payload. Les mots d'adresse portent un marqueur de
-- 2 octets dont la position tranche : `12c6`/`c0e2` en fin de mot => payload
-- little-endian, `c612`/`e2c0` en tête => big-endian. Repli sur l'heuristique
-- du port @16 (nul sur le seul record big-endian observé, 0x2a) quand aucun
-- endpoint ne porte de marqueur reconnu.
local function payload_is_le(tvb, plen, rec_port)
    for _, off in ipairs(EP_OFFSETS[plen] or {}) do
        local b = tvb(20 + off, 4):bytes()
        local hi = b:get_index(0) * 256 + b:get_index(1)
        local lo = b:get_index(2) * 256 + b:get_index(3)
        if lo == 0x12c6 or lo == 0xc0e2 then return true end
        if hi == 0xc612 or hi == 0xe2c0 then return false end
    end
    return rec_port ~= 0
end

-- ------------------------------------------------------------- dissecteur ---
function ray.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len < 8 then return 0 end

    pinfo.cols.protocol = "RAY5800"
    local subtree = tree:add(ray, tvb(), "Raymarine Discovery (ray5800)")

    local mtype = tvb(0, 4):le_uint()
    subtree:add_le(f.type, tvb(0, 4))

    -- ===== Annonces d'équipement (type 1 / 2) =====
    if mtype == 1 or mtype == 2 then
        if len < 32 then
            subtree:add_proto_expert_info(e_short)
            return len
        end
        -- @4 : rôle non résolu — varie selon le device ET le type de message
        -- (nœuds : 0x11 en type 1, 0x54/0x60 en type 2 ; radars : 0x4c/0x4d).
        subtree:add_le(f.u1, tvb(4, 4))
        local handle = tvb(8, 4):bytes():tohex()
        subtree:add(f.handle, tvb(8, 4))
        -- @12 : descriptor (type/modèle, partagé par devices identiques)
        --        = [octet0 sous-type][octet1=0][octets 2..3 mot de classe]
        local dtree = subtree:add_le(f.descriptor, tvb(12, 4))
        dtree:add(f.dev_subtype, tvb(12, 1))
        dtree:add_le(f.class_word, tvb(14, 2))
        local ip = ip_le(tvb(16, 4))
        subtree:add(f.ip, tvb(16, 4), ip)
        -- nom ASCIIZ : couper au 1er 0x00 ; les octets suivants sont un buffer
        -- réutilisé non nettoyé (ex. 'r' résiduel de "QuantumRadar" derrière "Quantum_W3").
        -- Le 0x00 est cherché à la main : stringz() lève « out of bounds » sur une
        -- trame tronquée dont la zone de 32 octets ne contient pas de terminateur.
        local nzone = tvb(20, math.min(32, len - 20))
        local nb = nzone:bytes()
        local nlen = nb:len()
        for i = 0, nb:len() - 1 do
            if nb:get_index(i) == 0 then nlen = i break end
        end
        local name = (nlen > 0) and tvb(20, nlen):string() or ""
        subtree:add(f.name, nzone, name)
        -- @52 : longueur de la queue, comptée à partir de @54. Vaut 2 pour le
        -- type 1 (56 o au total) et 16 pour le type 2 (70 o) : les deux types
        -- sont le même enregistrement, à queue de longueur variable.
        if len >= 56 then
            local trail_len = tvb(52, 2):le_uint()
            local ti = subtree:add_le(f.trail_len, tvb(52, 2))
            if trail_len ~= len - 54 then
                ti:add_proto_expert_info(e_len,
                    string.format("trail_len=%d but %d bytes remain from @54", trail_len, len - 54))
            end
            subtree:add(f.flag, tvb(54, 1))
        end
        -- Extension du type 2 : 14 octets à @56.
        if len >= 70 then
            local etree = subtree:add(f.ext, tvb(56, 14))
            etree:add_le(f.ext_id, tvb(56, 4))
            local variant = tvb(62, 1):uint()
            etree:add(f.ext_variant, tvb(62, 1))
            -- Variante radar : @64..69 porte un endpoint IP:PORT sur le bus interne
            -- RayNet, utile quand l'IP annoncée en @16 n'en est pas une (le radar
            -- annonce l'IP de son propre point d'accès). Le port recoupe celui de
            -- l'endpoint unicast du record de type 0 émis par le même handle.
            if variant == 0x02 then
                local eip = ip_le(tvb(64, 4))
                etree:add(f.raynet_ip, tvb(64, 4), eip)
                etree:add_le(f.ext_port, tvb(68, 2))
                etree:append_text(string.format(" — %s:%d", eip, tvb(68, 2):le_uint()))
            else
                etree:add_le(f.ext_port, tvb(68, 2))
            end
        end

        subtree:append_text(string.format(", %s%s (%s)",
            MSG_TYPES[mtype], (name ~= "" and " " .. name) or "", ip))
        pinfo.cols.info = string.format("DEVICE    %-12s %-16s handle=%s",
            (name ~= "" and name) or "(unnamed)", ip, handle)
        return len
    end

    -- ===== Annonces de service / canal (type 0) =====
    if mtype == 0 then
        if len < 20 then
            subtree:add_proto_expert_info(e_short)
            return len
        end
        local handle = tvb(4, 4):bytes():tohex()
        subtree:add(f.handle, tvb(4, 4))
        local rtype = tvb(8, 4):le_uint()
        subtree:add_le(f.rtype, tvb(8, 4))
        -- @12 se lit comme deux u16 : b vaut 30 chez les instruments, 100 chez
        -- les radars, 0 pour le record 0x2a.
        local ftree = subtree:add(f.flags1, tvb(12, 4))
        ftree:add_le(f.flags1_a, tvb(12, 2))
        ftree:add_le(f.flags1_b, tvb(14, 2))
        -- @16 : port du record. Chez les nœuds il vaut exactement le port de
        -- l'endpoint unicast de la payload (vérifié 40090/40090) ; chez les
        -- radars et sur 0x2a, non.
        local rec_port = tvb(16, 2):le_uint()
        subtree:add_le(f.rec_port, tvb(16, 2))
        local vlen = tvb(18, 2):le_uint()
        subtree:add_le(f.length, tvb(18, 2))

        local avail = len - 20
        if vlen ~= avail then
            subtree:add_proto_expert_info(e_len,
                string.format("length=%d but %d bytes remain", vlen, avail))
        end
        -- Octets réellement exploitables : la longueur annoncée borne la payload,
        -- même si la trame en contient davantage — les branches ci-dessous ne
        -- doivent pas décoder des octets que l'équipement dit ne pas en faire partie.
        local plen = math.min(vlen, avail)
        local little = payload_is_le(tvb, plen, rec_port)
        subtree:add(f.endian, tvb(16, 2), little and "little-endian" or "big-endian")
        local vrng = tvb(20, plen)
        local vtree = subtree:add(f.value, vrng)

        -- Un endpoint = [IP 4 o][port u16][extra u16], le tout dans l'ordre des
        -- octets de la payload. L'extra vaut 0 chez les nœuds (44 076/44 076) et
        -- varie chez les radars (records 0x28/0x29).
        local function add_endpoint(off, label)
            local ip    = little and ip_le(tvb(off, 4)) or ip_be(tvb(off, 4))
            local port  = little and tvb(off + 4, 2):le_uint() or tvb(off + 4, 2):uint()
            local extra = little and tvb(off + 6, 2):le_uint() or tvb(off + 6, 2):uint()
            local text  = string.format("%s:%d", ip, port)
            local dom   = domain_of(ip)
            local et = vtree:add(f.endpoint, tvb(off, 8), text)
            et:set_text(string.format("%s %s%s", label, text, dom and (" [" .. dom .. "]") or ""))
            et:add(f.ep_ip, tvb(off, 4), ip)
            et:add(f.ep_port, tvb(off + 4, 2), port)
            local xt = et:add(f.ep_extra, tvb(off + 6, 2), extra)
            if extra ~= 0 then xt:append_text(" [non-zero]") end
            return text
        end

        -- rtype 0x10 : 12 octets non résolus avant l'endpoint. Les deux u32 en
        -- +4/+8 valent 0x0803/0x0804, deux numéros de port sans IP associée.
        if plen == 20 then
            for _, off in ipairs({ 20, 24, 28 }) do
                vtree:add(f.unknown, tvb(off, 4),
                          little and tvb(off, 4):le_uint() or tvb(off, 4):uint())
            end
        end

        local eps = {}
        local offsets = EP_OFFSETS[plen]
        if offsets then
            for i, off in ipairs(offsets) do
                local label = (#offsets > 1) and string.format("Endpoint %d:", i) or "Endpoint:"
                eps[#eps + 1] = add_endpoint(20 + off, label)
            end
        end

        local eptxt = table.concat(eps, " ")
        subtree:append_text(string.format(", Service rtype=0x%02x %s (handle %s)",
            rtype, eptxt, handle))
        pinfo.cols.info = string.format("CHANNEL   rtype=0x%02x %s %-37s handle=%s",
            rtype, little and "LE" or "BE", eptxt, handle)
        return len
    end

    -- Type inconnu
    subtree:append_text(string.format(", Unknown type %d", mtype))
    pinfo.cols.info = string.format("type=%d (unknown)", mtype)
    return len
end

-- Enregistrement sur le port UDP 5800
local udp_port = DissectorTable.get("udp.port")
udp_port:add(5800, ray)
