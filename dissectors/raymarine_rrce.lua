--
-- raymarine_rrce.lua — Dissecteur Wireshark pour le protocole d'entrée de la
--                      télécommande Raymarine (RRCE, magie "ECRR", TCP 50000).
--
-- RRCE = Raymarine Remote Control (Equipment). La magie 4 octets sur le fil est
-- littéralement "ECRR" (45 43 52 52) — c'est la signature du protocole, gardée
-- telle quelle ; RRCE est le nom de l'outillage.
--
-- RayConnect affiche l'écran du MFD (vidéo RTSP 8554) et renvoie sur ce
-- canal les entrées de l'utilisateur : touchers de l'écran, boutons de façade et
-- crans de molette. Le MFD écoute sur 50000 ; le flux est unidirectionnel
-- (client → MFD), sans réponse applicative.
--
-- En-tête commun, 9 octets :
--   "ECRR"     magie (45 43 52 52)
--   01         version de l'en-tête (toujours 01)
--   xx         version du protocole RRC, annoncée en mDNS par la clé TXT
--              `raymarine-mfd-rrc-version` (0a ou 00 selon le client)
--   xx         type de record : 01 = boutons, 02 = molette, 03 = écran tactile
--   len        longueur de la charge utile (06 tactile, 02 bouton, 04 molette)
--   00         réservé
--
-- Le type tient dans le seul octet [6] : trois constructeurs de
-- trames ne différant que par cet octet et la longueur, l'octet [5] étant une
-- variable statique commune aux trois. C'est pourquoi `rrce.pcap` porte 0a pour
-- le tactile et 00 pour les boutons dans un même flux TCP.
--
-- Charge utile tactile (6 octets, record de 15 en tout) :
--   op   u8   1=DOWN 2=MOVE 3=UP (4=CANCEL, non observé)
--   fid  u8   identifiant de doigt (0/1 : multi-touch)
--   X    u16 LE  coordonnée X normalisée 0..65535
--   Y    u16 LE  coordonnée Y normalisée 0..65535
--
-- Charge utile bouton (2 octets, record de 11 en tout) :
--   code u8   bouton de façade (codes de touches virtuelles Windows)
--   état u8   1=enfoncé (réémis à ~120 Hz tant que tenu) 2=relâché
--
-- Charge utile molette (4 octets, record de 13 en tout) :
--   delta i16 LE  incrément signé du cran (|delta| >= 24)
--   cumul i16 LE  somme des delta depuis le début de la salve ; le record
--                 d'ouverture porte cumul = 0 et ne s'applique pas
--
-- Spécification : voir docs/3. protocole-rrce-50000.md.
--
-- Installation :
--   Copier dans ~/.local/lib/wireshark/plugins/ (macOS/Linux) puis recharger.
-- Test :
--   tshark -X lua_script:raymarine_rrce.lua -r capture.pcapng -Y rrce -O rrce
--   tshark -X lua_script:raymarine_rrce.lua -r capture.pcapng -Y 'rrce.key'
--

local rrce = Proto("rrce", "Raymarine Remote Control (RRCE, magie ECRR, TCP 50000)")

local HDR_LEN = 9                    -- magie + version + source + len + réservé
local MAGIC = "ECRR"                 -- magie littérale sur le fil (inchangée)

local TYPE_KEY, TYPE_WHEEL, TYPE_TOUCH = 1, 2, 3   -- octet [6] de l'en-tête
local TYPES = {
    [TYPE_KEY] = "Keypad", [TYPE_WHEEL] = "Wheel", [TYPE_TOUCH] = "Touchscreen",
}

local OPS = { [1] = "DOWN", [2] = "MOVE", [3] = "UP", [4] = "CANCEL" }
local STATES = { [1] = "pressed", [2] = "released" }

-- Boutons de façade. RayConnect réutilise les codes de touches virtuelles
-- Windows : F7/F8/F9/F11, Échap, Page↑/↓, Entrée, flèches.
local KEYS = {
    [0x0d] = "OK",     [0x1b] = "BACK",
    [0x21] = "ZOOM-",  [0x22] = "ZOOM+",
    [0x25] = "LEFT",   [0x26] = "UP",    [0x27] = "RIGHT", [0x28] = "DOWN",
    [0x76] = "HOME",   [0x77] = "WPT",   [0x78] = "MENU",  [0x7a] = "SWITCH",
}

local f = rrce.fields
f.magic   = ProtoField.string("rrce.magic",   "Magic")
f.hdr     = ProtoField.bytes ("rrce.header",  "Header")
f.version = ProtoField.uint8 ("rrce.version", "Header version", base.DEC)
f.rrcver  = ProtoField.uint8 ("rrce.rrc_version", "RRC version", base.HEX)
f.type    = ProtoField.uint8 ("rrce.type",    "Record type", base.DEC, TYPES)
f.len     = ProtoField.uint8 ("rrce.len",     "Payload length", base.DEC)
f.op      = ProtoField.uint8 ("rrce.op",      "Op",       base.DEC, OPS)
f.finger  = ProtoField.uint8 ("rrce.finger",  "Finger id", base.DEC)
f.x       = ProtoField.uint16("rrce.x",       "X (0..65535)", base.DEC)
f.y       = ProtoField.uint16("rrce.y",       "Y (0..65535)", base.DEC)
f.xpct    = ProtoField.string("rrce.x_pct",   "X %")
f.ypct    = ProtoField.string("rrce.y_pct",   "Y %")
f.key     = ProtoField.uint8 ("rrce.key",     "Key", base.HEX, KEYS)
f.state   = ProtoField.uint8 ("rrce.key_state", "State", base.DEC, STATES)
f.delta   = ProtoField.int16 ("rrce.wheel_delta", "Wheel delta", base.DEC)
f.total   = ProtoField.int16 ("rrce.wheel_total", "Wheel total", base.DEC)
f.payload = ProtoField.bytes ("rrce.payload", "Payload")

local e_magic = ProtoExpert.new("rrce.bad_magic",
    "Missing ECRR magic (desync?)", expert.group.MALFORMED, expert.severity.WARN)
local e_src = ProtoExpert.new("rrce.unknown_source",
    "Unknown record type", expert.group.UNDECODED, expert.severity.NOTE)
rrce.experts = { e_magic, e_src }

-- Dissèque un record complet (en-tête + charge utile) ; retourne un résumé court.
local function dissect_record(rng, tree)
    local typ = rng(6, 1):uint()     -- type de record ; [5] n'est qu'une version
    local n   = rng(7, 1):uint()
    local summary

    if typ == TYPE_TOUCH and n >= 6 then
        local op  = rng(9, 1):uint()
        local fid = rng(10, 1):uint()
        local x   = rng(11, 2):le_uint()
        local y   = rng(13, 2):le_uint()
        summary = string.format("%s f%d (%d,%d)", OPS[op] or ("op" .. op), fid, x, y)
    elseif typ == TYPE_KEY and n >= 2 then
        local code = rng(9, 1):uint()
        local st   = rng(10, 1):uint()
        summary = string.format("KEY %s %s", KEYS[code] or string.format("0x%02x", code),
                                STATES[st] or ("state" .. st))
    elseif typ == TYPE_WHEEL and n >= 4 then
        local delta = rng(9, 2):le_int()
        local total = rng(11, 2):le_int()
        summary = (total == 0) and "WHEEL burst start"
            or string.format("WHEEL %+d (total %+d)", delta, total)
    else
        summary = string.format("type %02x (%d octets)", typ, n)
    end

    local item = tree:add(rrce, rng(), "RRCE " .. summary)
    item:add(f.magic,   rng(0, 4))
    item:add(f.hdr,     rng(4, 5))
    item:add(f.version, rng(4, 1))
    item:add(f.rrcver,  rng(5, 1))
    item:add(f.type,    rng(6, 1))
    item:add(f.len,     rng(7, 1))

    if typ == TYPE_TOUCH and n >= 6 then
        local x = rng(11, 2):le_uint()
        local y = rng(13, 2):le_uint()
        item:add(f.op,     rng(9, 1))
        item:add(f.finger, rng(10, 1))
        item:add_le(f.x,   rng(11, 2))
        item:add_le(f.y,   rng(13, 2))
        item:add(f.xpct, rng(11, 2), string.format("%.1f%%", x * 100.0 / 65535))
        item:add(f.ypct, rng(13, 2), string.format("%.1f%%", y * 100.0 / 65535))
    elseif typ == TYPE_KEY and n >= 2 then
        item:add(f.key,   rng(9, 1))
        item:add(f.state, rng(10, 1))
    elseif typ == TYPE_WHEEL and n >= 4 then
        item:add_le(f.delta, rng(9, 2))
        item:add_le(f.total, rng(11, 2))
    elseif n > 0 then
        -- Type inconnu : on montre la charge utile brute plutôt que rien.
        item:add(f.payload, rng(9, n))
        item:add_proto_expert_info(e_src)
    end
    return summary
end

function rrce.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len == 0 then return 0 end
    pinfo.cols.protocol = "RRCE"

    local offset, first, count = 0, nil, 0
    while offset < len do
        -- en-tête incomplet : on ne connaît pas encore la longueur du record
        if len - offset < HDR_LEN then
            pinfo.desegment_offset = offset
            pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
            break
        end
        -- vérifier la magie ; sinon on n'est pas (ou plus) aligné
        if tvb(offset, 4):string() ~= MAGIC then
            tree:add_proto_expert_info(e_magic)
            break
        end
        -- charge utile incomplète : demander le réassemblage
        local reclen = HDR_LEN + tvb(offset + 7, 1):uint()
        if len - offset < reclen then
            pinfo.desegment_offset = offset
            pinfo.desegment_len = reclen - (len - offset)
            break
        end
        local s = dissect_record(tvb(offset, reclen), tree)
        count = count + 1
        if not first then first = s end
        offset = offset + reclen
    end

    if first then
        pinfo.cols.info = (count > 1)
            and string.format("%s  (+%d)", first, count - 1) or first
    end
    return len
end

DissectorTable.get("tcp.port"):add(50000, rrce)
