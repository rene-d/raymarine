--
-- raymarine_raydb.lua — Dissecteur Wireshark pour le protocole Raymarine
--                       "RayDB" (bus de souscription clé/valeur, TCP 23333).
--
-- RayDB est un bus publish/subscribe : le client se connecte, s'annonce
-- (HELLO), s'abonne à des chemins ("data/#", "diag/mfd/.../version"…) et le
-- MFD pousse les valeurs (UPDATE) au fil de l'eau.
--
-- Format des trames (little-endian) :
--   [u32 len]        longueur du reste de la trame (octets qui suivent len)
--   [u32 msg_type]   toujours 1 observé
--   [u8  op]         3=SUBSCRIBE 4=UPDATE 5/6=ACK 7=HELLO 0=?
--   [u32 path_len]   longueur de la chaîne "chemin"
--   [u32 pad]        réservé / flags (0 observé)
--   [path_len oct.]  chemin ASCII
--   [bloc valeur]    présent surtout sur UPDATE (voir dissect_value)
--
-- Bloc valeur (UPDATE), entièrement little-endian :
--   [3 octets réservés][u32 type][valeur][4 octets de padding]
--     type 0x00  booléen  : 1 octet
--     type 0x01  i8       : 1 octet
--     type 0x02  i16      : 2 octets
--     type 0x03  i32      : 4 octets
--     type 0x04  i64      : 8 octets
--     type 0x07  entier   : u32
--     type 0x09  float32
--     type 0x0a  double
--     type 0x0b  chaîne   : [u64 len][octets]
--     type 0x0d  liste    : [u64 n] puis n × ([u32 type][valeur])
--     type 0x0e  table    : [u64 n] puis n × ([u64 klen][clé][u32 type][valeur])
--
-- La valeur suit immédiatement son type, sans champ de longueur : seules les
-- chaînes en portent un (le [u64 len] du type 0x0b). Listes et tables
-- s'imbriquent.
--
-- Le 0x0e n'est pas une « valeur nommée » : c'est une table de n entrées, et n
-- vaut rarement 1. Le cas n=1 est celui des `diag/…`, où la clé
-- redouble le plus souvent le dernier segment du chemin ; on ne l'affiche alors
-- que si elle en diffère.
--
-- Unités observées : angles en radians, vitesses en m/s, profondeurs en mètres ;
-- data/position est une chaîne "latitude,longitude".
--
-- Un segment TCP peut contenir plusieurs trames concaténées, et une trame
-- peut s'étaler sur plusieurs segments : le dissecteur gère le réassemblage.
--
-- Installation :
--   Copier dans le dossier des plugins Lua personnels de Wireshark :
--     macOS/Linux : ~/.local/lib/wireshark/plugins/  (ou ~/.config/wireshark/plugins/)
--     Windows     : %APPDATA%\Wireshark\plugins\
--   puis Analyze > Reload Lua Plugins.
--
-- Test en ligne de commande :
--   tshark -X lua_script:raymarine_raydb.lua -r capture.pcapng \
--          -Y "raydb" -O raydb
--
-- Spécification : voir raydb_decode.py / docs/2. protocole-raydb-23333.md
--

local raydb = Proto("raydb", "Raymarine RayDB (TCP 23333)")

-- ------------------------------------------------------------------ champs --
-- Seuls 3/4/5/6/7 existent : les opcodes 0/100/115 qu'on croyait voir venaient
-- d'un découpage sans réassemblage TCP (cf. § 4 de la spec).
local OPS = {
    [3]   = "SUBSCRIBE",
    [4]   = "UPDATE",
    [5]   = "ACK",
    [6]   = "ACK",
    [7]   = "HELLO",
}

local VTYPES = {
    [0x00] = "bool",
    [0x01] = "int8",
    [0x02] = "int16",
    [0x03] = "int32",
    [0x04] = "int64",
    [0x05] = "uint8",
    [0x06] = "uint16",
    [0x07] = "uint32",
    [0x08] = "uint64",
    [0x09] = "float32",
    [0x0a] = "double",
    [0x0b] = "string",
    [0x0d] = "list",
    [0x0e] = "map",
}

local f = raydb.fields
f.len       = ProtoField.uint32("raydb.len",       "Frame length",   base.DEC)
f.msg_type  = ProtoField.uint32("raydb.msg_type",  "Message type",   base.DEC)
f.op        = ProtoField.uint8 ("raydb.op",        "Op",             base.DEC, OPS)
f.path_len  = ProtoField.uint32("raydb.path_len",  "Path length",    base.DEC)
f.pad       = ProtoField.uint32("raydb.pad",       "Reserved",       base.HEX)
f.path      = ProtoField.string("raydb.path",      "Path")

f.value     = ProtoField.bytes ("raydb.value",     "Value block")
f.vtype     = ProtoField.uint32("raydb.vtype",     "Value type",     base.HEX, VTYPES)
f.vbool     = ProtoField.bool  ("raydb.vbool",     "Value (bool)")
f.vint      = ProtoField.int32 ("raydb.vint",      "Value (int)",    base.DEC)
f.vint64    = ProtoField.int64 ("raydb.vint64",    "Value (int64)",  base.DEC)
f.vuint     = ProtoField.uint32("raydb.vuint",     "Value (uint32)", base.DEC)
f.vuint64   = ProtoField.uint64("raydb.vuint64",   "Value (uint64)", base.DEC)
f.vcount    = ProtoField.uint32("raydb.vcount",    "Entry count",    base.DEC)
f.vfloat    = ProtoField.float ("raydb.vfloat",    "Value (float)")
f.vdouble   = ProtoField.double("raydb.vdouble",   "Value (double)")
f.vstr      = ProtoField.string("raydb.vstr",      "Value (string)")
f.vname     = ProtoField.string("raydb.vname",     "Value name")

-- expert infos
local e_short = ProtoExpert.new("raydb.too_short",
    "Truncated RayDB frame", expert.group.MALFORMED, expert.severity.WARN)
local e_badtype = ProtoExpert.new("raydb.bad_msg_type",
    "Unexpected message type (expected 1)", expert.group.PROTOCOL, expert.severity.NOTE)
raydb.experts = { e_short, e_badtype }

-- --------------------------------------------------------- bloc valeur -----
-- Largeur des types à taille fixe.
local WIDTH = {
    [0x00] = 1, [0x01] = 1, [0x02] = 2, [0x03] = 4, [0x04] = 8,
    [0x05] = 1, [0x06] = 2, [0x07] = 4, [0x08] = 8, [0x09] = 4, [0x0a] = 8,
}

-- Au-delà, le résumé de l'en-tête est coupé : une table de `sf/…` porte 27
-- entrées, que la colonne Info n'a pas à recevoir en entier.
local SUMMARY_MAX = 200

-- Déclaration anticipée : listes et tables se réimbriquent.
local dissect_at

-- Décode une valeur typée à l'offset `o`. La valeur suit immédiatement son
-- type : seules les chaînes portent une longueur ([u64 len][octets]).
-- Retourne (résumé, offset de fin), ou nil si le type est inconnu ou le bloc
-- trop court.
function dissect_at(rng, o, vtype, tree)
    local len = rng:len()
    local w = WIDTH[vtype]
    if w and o + w > len then return nil end

    if vtype == 0x00 then                                -- booléen
        tree:add(f.vbool, rng(o, 1))
        return (rng(o, 1):uint() ~= 0) and "true" or "false", o + 1

    elseif vtype == 0x01 or vtype == 0x02 or vtype == 0x03 then   -- i8/i16/i32
        tree:add_le(f.vint, rng(o, w))
        return tostring(rng(o, w):le_int()), o + w

    elseif vtype == 0x04 then                            -- i64
        tree:add_le(f.vint64, rng(o, 8))
        return tostring(rng(o, 8):le_int64()), o + 8

    elseif vtype == 0x05 or vtype == 0x06 or vtype == 0x07 then   -- u8/u16/u32
        tree:add_le(f.vuint, rng(o, w))
        return tostring(rng(o, w):le_uint()), o + w

    elseif vtype == 0x08 then                            -- u64
        tree:add_le(f.vuint64, rng(o, 8))
        return tostring(rng(o, 8):le_uint64()), o + 8

    elseif vtype == 0x09 then                            -- float32
        tree:add_le(f.vfloat, rng(o, 4))
        return string.format("%.6g", rng(o, 4):le_float()), o + 4

    elseif vtype == 0x0a then                            -- double
        tree:add_le(f.vdouble, rng(o, 8))
        return string.format("%.6g", rng(o, 8):le_float()), o + 8

    elseif vtype == 0x0b then                            -- [u64 len][octets]
        if o + 8 > len then return nil end
        local slen = rng(o, 4):le_uint()                 -- longueurs << 2^32
        if slen == 0 then                                -- chaîne vide : on
            tree:add(f.vstr, rng(o, 8), "")              -- pointe la longueur
            return '""', o + 8
        end
        if o + 8 + slen > len then return nil end
        local s = rng(o + 8, slen):string()
        tree:add(f.vstr, rng(o + 8, slen), s)
        return '"' .. s .. '"', o + 8 + slen

    elseif vtype == 0x0d or vtype == 0x0e then           -- liste / table
        if o + 8 > len then return nil end
        local n = rng(o, 4):le_uint()                    -- compteurs << 2^32
        if n > len then return nil end                   -- compteur aberrant
        tree:add_le(f.vcount, rng(o, 4))
        o = o + 8
        local parts = {}
        for _ = 1, n do
            local key = nil
            if vtype == 0x0e then                        -- table : clé devant
                if o + 8 > len then return nil end
                local klen = rng(o, 4):le_uint()
                o = o + 8
                if o + klen > len then return nil end
                key = klen > 0 and rng(o, klen):string() or ""
                if klen > 0 then tree:add(f.vname, rng(o, klen), key) end
                o = o + klen
            end
            if o + 4 > len then return nil end
            local summary, e = dissect_at(rng, o + 4, rng(o, 4):le_uint(), tree)
            if not summary then return nil end
            o = e
            parts[#parts + 1] = key and (key .. ": " .. summary) or summary
        end
        local joined = table.concat(parts, ", ")
        if #joined > SUMMARY_MAX then
            joined = joined:sub(1, SUMMARY_MAX) .. "…"
        end
        if vtype == 0x0e then return "{" .. joined .. "}", o end
        return "[" .. joined .. "]", o
    end

    return nil
end

-- Décode le bloc valeur d'un UPDATE dans son propre sous-arbre.
-- `rng` couvre tout le bloc valeur (à partir des 3 octets réservés) ; `leaf`
-- est le dernier segment du chemin, pour ne pas répéter la clé qui le double.
-- Retourne une courte chaîne résumé (ou nil).
local function dissect_value(rng, tree, leaf)
    local len = rng:len()
    if len < 7 then
        tree:add(f.value, rng)
        return nil
    end

    local vtype = rng(3, 4):le_uint()
    tree:add_le(f.vtype, rng(3, 4))

    -- Table à entrée unique — la forme des `diag/…` : la clé y redouble le plus
    -- souvent le dernier segment du chemin, qu'on n'affiche donc pas deux fois.
    if vtype == 0x0e and len >= 24 and rng(7, 4):le_uint() == 1 then
        -- [u64 count=1 @7][u64 klen @15][clé @23][u32 type][valeur]
        local klen = rng(15, 4):le_uint()                -- longueurs << 2^32
        local o = 23
        if klen == 0 or o + klen + 4 > len then
            tree:add(f.value, rng)
            return nil
        end
        local key = rng(o, klen):string()
        tree:add(f.vname, rng(o, klen), key)
        o = o + klen
        local summary = dissect_at(rng, o + 4, rng(o, 4):le_uint(), tree)
        if not summary then return key end
        if key == leaf then return summary end
        return string.format("%s=%s", key, summary)
    end

    local summary = dissect_at(rng, 7, vtype, tree)      -- valeur nue
    if summary then return summary end

    tree:add(f.value, rng)
    return nil
end

-- ---------------------------------------------------- une trame RayDB ------
-- `rng` couvre exactement une trame complète (len inclus). Retourne un résumé.
local function dissect_pdu(rng, pinfo, tree)
    local flen  = rng(0, 4):le_uint()
    local op    = rng(8, 1):uint()
    local plen  = rng(9, 4):le_uint()
    local opname = OPS[op] or string.format("op%d", op)

    local item = tree:add(raydb, rng(), string.format("RayDB %s", opname))
    item:add_le(f.len,      rng(0, 4))
    local mt = rng(4, 4):le_uint()
    local mt_item = item:add_le(f.msg_type, rng(4, 4))
    if mt ~= 1 then mt_item:add_proto_expert_info(e_badtype) end
    item:add_le(f.op,       rng(8, 1))
    item:add_le(f.path_len, rng(9, 4))
    item:add_le(f.pad,      rng(13, 4))

    local path = ""
    if 17 + plen <= rng:len() then
        path = rng(17, plen):string()
        item:add(f.path, rng(17, plen), path)
    else
        item:add_proto_expert_info(e_short)
    end

    local summary = nil
    local voff = 17 + plen
    if op == 4 and voff < rng:len() then
        local vrng = rng(voff, rng:len() - voff)
        local vtree = item:add(raydb, vrng, "Value")
        summary = dissect_value(vrng, vtree, path:match("([^/]*)$") or "")
    end

    item:append_text(string.format(", %s %s%s", opname, path,
        summary and ("  = " .. summary) or ""))
    return string.format("%s %s%s", opname, path,
        summary and ("=" .. summary) or "")
end

-- ------------------------------------------------ dissecteur principal -----
-- Gère plusieurs trames par segment + réassemblage des trames coupées.
function raydb.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len == 0 then return 0 end

    pinfo.cols.protocol = "RayDB"
    local offset = 0
    local first_summary, count = nil, 0

    while offset < len do
        -- besoin des 4 octets de longueur
        if len - offset < 4 then
            pinfo.desegment_offset = offset
            pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
            break
        end
        local flen = tvb(offset, 4):le_uint()
        -- garde-fou : une trame RayDB a au moins msg_type+op+path_len+pad
        if flen < 13 then break end
        local total = 4 + flen
        -- trame incomplète : demander le réassemblage
        if len - offset < total then
            pinfo.desegment_offset = offset
            pinfo.desegment_len = total - (len - offset)
            break
        end

        local s = dissect_pdu(tvb(offset, total), pinfo, tree)
        count = count + 1
        if not first_summary then first_summary = s end
        offset = offset + total
    end

    if first_summary then
        if count > 1 then
            pinfo.cols.info = string.format("%s  (+%d)", first_summary, count - 1)
        else
            pinfo.cols.info = first_summary
        end
    end
    return len
end

-- Enregistrement sur le port TCP 23333
local tcp_port = DissectorTable.get("tcp.port")
tcp_port:add(23333, raydb)
