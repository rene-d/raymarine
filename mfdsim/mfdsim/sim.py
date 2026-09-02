"""
sim.py — bateau virtuel alimentant les chemins `data/…` de RayDB.

Produit une navigation plausible plutôt qu'un rejeu : le bateau avance en
estime (dead reckoning) sur un cap qui serpente, avec houle (roulis/tangage),
vent apparent recalculé depuis le vent réel, et sondeur qui suit un fond
irrégulier. Les valeurs restent ainsi cohérentes entre elles — un client qui
convertit en NMEA (`raydb_client.py --nmea`) obtient des phrases qui se tiennent.

**Unités RayDB** (cf. « 2. protocole-raydb-23333.md ») : angles en **radians**,
vitesses en **m/s**, profondeurs en **mètres**, et `data/position` est la chaîne
`"latitude,longitude"`. La simulation travaille donc nativement en SI ; les
constantes lisibles (nœuds, degrés) sont converties à la construction.

Les référentiels suivent la convention relevée sur le MFD réel :
`data/wind/direction/{true,apparent}` sont des angles **relatifs à l'étrave**,
pas des directions référencées au nord.
"""
from __future__ import annotations

import math
import random
import threading
import time
import unicodedata

# Types RayDB du bloc valeur (cf. §5.1 de la spec).
T_FLOAT = 0x09
T_DOUBLE = 0x0A
T_STRING = 0x0B

KNOT = 1852.0 / 3600.0   # m/s
DEG = math.pi / 180.0    # radians
EARTH_R = 6371000.0      # m, rayon moyen — suffisant pour de l'estime

DEFAULT_SPEED = 7.1          # vitesse surface visée, en nœuds (option --speed)
DEFAULT_LATITUDE = 48.3206   # position initiale
DEFAULT_LONGITUDE = -4.8043
DEFAULT_HEADING = 249.0      # cap vrai visé, en degrés (option --heading)

# Allures : angle du vent réel à l'étrave, en degrés. C'est *l'allure* qui donne
# le TWA — choisir « travers » pose le vent à 90° de l'étrave, et le vent
# apparent s'en déduit par composition avec la vitesse du bateau.
POINTS_OF_SAIL = {
    "pres": 45.0,
    "reaching": 60.0,
    "travers": 90.0,
    "largue": 120.0,
    "grand-largue": 150.0,
}
DEFAULT_POINT_OF_SAIL = "reaching"
# Tolérance de l'étiquetage inverse (TWA → nom d'allure), en degrés : au-delà,
# l'angle ne porte plus de nom et `status()` n'en invente pas.
POINT_OF_SAIL_TOLERANCE = 15.0
# Amure : signe du TWA. Positif = vent de tribord, comme le 55° d'origine.
TACKS = {"tribord": 1.0, "starboard": 1.0, "babord": -1.0, "port": -1.0}
# Vent réel visé, en proportion de la vitesse du bateau : un voilier qui tient
# 7 nœuds au reaching en a une bonne quinzaine — l'ordre de grandeur retenu.
TWS_FACTOR = 2.0

# Mouillage simulé par `AnchorSim` (option --anchor), côte nord de Bretagne.
DEFAULT_ANCHOR_LAT = 48.65428559140165
DEFAULT_ANCHOR_LON = -3.879216007615262
DEFAULT_SWING_MIN = 30.0     # m, rayon d'évitage, chaîne au plus court
DEFAULT_SWING_MAX = 40.0     # m, chaîne au plus long
DEFAULT_ANCHOR_DEPTH = 6.5   # m, fond sous la quille au mouillage
DEFAULT_DRAG_SPEED = 0.5     # nœuds, vitesse de dérive au dérapage
# Un tour complet autour de l'ancre, en secondes : le bateau évite au rythme de
# la renverse. À 35 m de rayon, 20 min donnent ~0,35 nœud, l'ordre de grandeur
# d'un bateau qui évite.
DEFAULT_SWING_PERIOD = 1200.0
# Plafond de vitesse quand le bateau rejoint son cercle d'évitage.
MAX_SWING_SPEED = 1.5 * KNOT
# Pas de temps maximal d'un `step()`, en secondes (cf. `BoatSim._tick`).
MAX_DT = 1.0
# Cycle de marée, comprimé pour rester observable le temps d'un essai. Il pilote
# à la fois la sonde et le rayon d'évitage, qui en dépend.
TIDE_PERIOD = 240.0
# Filtrage du vecteur vitesse dont sortent SOG et COG au mouillage : plus le
# rappel est lent, plus la route est stable. 0,08 /s ≈ une douzaine de secondes
# de mémoire, ce qui suffit à noyer le bruit de position sans figer la giration.
COG_FILTER = 0.08
# En dessous, la route n'est plus qu'un artefact : on garde la dernière.
COG_FLOOR = 0.05 * KNOT
# Giration maximale, en rad/s : un bateau ne pivote pas d'un bloc, il abat. À
# 6 °/s, un demi-tour commandé depuis l'API prend une demi-minute.
MAX_TURN_RATE = 6.0 * DEG
# Barre : l'angle suit la giration, pour qu'un changement de cap se lise aussi
# dans `data/rudder`. Le gain est calé pour qu'une giration pleine demande une
# vingtaine de degrés de barre, et la tenue de route, un ou deux.
RUDDER_GAIN = 4.0
MAX_RUDDER = 30.0 * DEG


def _wrap(angle: float) -> float:
    """Repliement d'un angle dans [-π, π[ — la forme signée des angles relatifs."""
    return ((angle + math.pi) % (2 * math.pi)) - math.pi


def _slug(text: object) -> str:
    """Minuscules sans accent, tirets pour les blancs : « Grand largue » → grand-largue."""
    folded = unicodedata.normalize("NFD", str(text).strip().lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return "-".join(folded.replace("_", " ").split())


def point_of_sail(value: object) -> float:
    """Allure → angle du vent réel à l'étrave, en degrés.

    Accepte un nom d'allure (`travers`, `grand largue`…) ou directement un angle
    en degrés, éventuellement négatif : le signe vaut alors bâbord amure.
    """
    name = _slug(value)
    if name in POINTS_OF_SAIL:
        return POINTS_OF_SAIL[name]
    try:
        angle = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"allure inconnue : {value!r} — "
                         f"{', '.join(POINTS_OF_SAIL)}, ou un angle en degrés") from None
    if not -180.0 <= angle <= 180.0:
        raise ValueError(f"allure hors [-180, 180] degrés : {angle}")
    return angle


def check_position(lat: object, lon: object) -> tuple[float, float]:
    """Valide un couple de coordonnées et le renvoie en flottants."""
    lat, lon = float(lat), float(lon)  # type: ignore[arg-type]
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude hors [-90, 90] : {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude hors [-180, 180] : {lon}")
    return lat, lon


def tack_sign(value: object) -> float:
    """Amure → signe du TWA (+1 tribord, -1 bâbord)."""
    try:
        return TACKS[_slug(value)]
    except KeyError:
        raise ValueError(f"amure inconnue : {value!r} — "
                         f"{', '.join(TACKS)}") from None


class BoatSim:
    """État de navigation d'un bateau virtuel, échantillonné dans le temps.

    `snapshot()` renvoie l'état complet (utilisé pour l'envoi « retained » à la
    souscription) ; `step()` fait avancer le temps et renvoie les seuls chemins
    dont la valeur a bougé, ce qui reproduit le comportement du MFD : un UPDATE
    est poussé au changement, pas à cadence fixe.
    """

    def __init__(self, lat: float = DEFAULT_LATITUDE, lon: float = DEFAULT_LONGITUDE,
                 speed: float = DEFAULT_SPEED, heading: float = DEFAULT_HEADING,
                 allure: object = DEFAULT_POINT_OF_SAIL, seed: int | None = None) -> None:
        # Départ en mer d'Iroise, entre Ouessant et le chenal du Four.
        self.lat = lat
        self.lon = lon
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()

        # Consignes et valeurs instantanées vont par paires : le bateau revient
        # sans cesse vers la consigne en serpentant autour (voir `step`), et
        # c'est la consigne — elle seule — que déplacent la ligne de commande
        # et l'API de contrôle.
        #
        #   consigne          instantané
        #   self.course       self.heading            cap vrai
        #   self.speed        self.stw                vitesse surface
        #   self.wind_speed   self.wind_true_speed    vent réel
        #   self.wind_angle   self.wind_true_angle    vent réel à l'étrave (TWA)
        #
        # Tout le reste en découle — SOG par composition avec le courant, vent
        # apparent par composition avec la vitesse du bateau.
        self.course = self.heading = (heading * DEG) % (2 * math.pi)
        self.speed = self.stw = speed * KNOT
        self.wind_speed = self.wind_true_speed = TWS_FACTOR * self.speed
        self.wind_angle = self.wind_true_angle = point_of_sail(allure) * DEG

        self.variation = 1.4 * DEG      # déclinaison magnétique
        self.depth = 24.0

        # Consignes posées depuis le thread de l'API de contrôle, lues par
        # `step()` dans celui de RayDB.
        self._lock = threading.Lock()

        # Statistiques cumulées, publiées sous data/{sog,stw}/{avg,max}.
        self._sog_sum = 0.0
        self._sog_n = 0
        self._sog_max = 0.0
        self._stw_max = 0.0
        self._depth_min = self.depth
        self._depth_max = self.depth

        self._last = time.monotonic()
        self._prev: dict[str, tuple[int, object]] = {}

        # Grandeurs dérivées, calculées par `step()` et lues par `_state()`.
        # Les tenir sur l'instance plutôt qu'en variables locales permet à une
        # sous-classe de réimplémenter `step()` sans recopier la table des
        # chemins (cf. `AnchorSim`).
        self.sog = self.cog = 0.0
        self.wind_app_speed = self.wind_app_angle = 0.0
        self.roll = self.pitch = self.yaw = self.rot = self.rudder = 0.0
        self.tide_set = self.tide_drift = 0.0
        self.accuracy = 1.8

    # -------------------------------------------------------- commandes -----
    # Les commandes déplacent la **consigne**, pas la valeur instantanée : le
    # bateau abat, accélère ou change d'allure en quelques secondes au lieu de
    # voir ses instruments sauter, et `data/rot` comme `data/rudder` racontent
    # la manœuvre. `status()` publie les deux, si bien qu'un ordre reste lisible
    # avant même d'avoir pris effet. La position, elle, n'a pas de consigne : la
    # déplacer *est* une téléportation, assumée comme telle.
    #
    # La simulation n'avançant que sur demande d'un client RayDB (cf. `_tick`),
    # rien ne bouge tant qu'aucun client n'est connecté — les consignes sont
    # alors seulement mémorisées.
    def set_position(self, lat: float, lon: float) -> dict[str, object]:
        """Téléporte le bateau. Le cap et la vitesse ne changent pas."""
        lat, lon = check_position(lat, lon)
        with self._lock:
            self.lat, self.lon = lat, lon
        return self.status()

    def set_heading(self, heading: float) -> dict[str, object]:
        """Change le cap vrai visé, en degrés. Le bateau y abat (`MAX_TURN_RATE`)."""
        with self._lock:
            self.course = (float(heading) * DEG) % (2 * math.pi)
        return self.status()

    def set_speed(self, speed: float) -> dict[str, object]:
        """Change la vitesse surface visée, en nœuds, et le vent réel avec.

        Le vent suit la vitesse (`TWS_FACTOR`) : c'est lui qui fait avancer le
        bateau, un voilier qui tient 7 nœuds n'en a pas 3 de vent. Sans ce
        couplage, demander 15 nœuds sous 14 de brise donnerait un vent apparent
        de dos à une allure de près — incohérent pour un client NMEA.
        """
        speed = float(speed)
        if speed < 0.0:
            raise ValueError(f"vitesse négative : {speed}")
        with self._lock:
            self.speed = speed * KNOT
            self.wind_speed = TWS_FACTOR * self.speed
        return self.status()

    def set_sail(self, allure: object = None, amure: object = None) -> dict[str, object]:
        """Change l'allure, donc le TWA, donc le vent apparent.

        `allure` est un nom (`pres`, `travers`, `grand largue`…) ou un angle en
        degrés ; `amure` (`tribord`/`babord`) choisit le bord, à défaut celui en
        cours — un angle négatif vaut bâbord amure.
        """
        with self._lock:
            angle = self.wind_angle / DEG if allure is None else point_of_sail(allure)
            if amure is not None:
                sign = tack_sign(amure)
            elif angle < 0.0:
                sign = -1.0
            else:
                sign = math.copysign(1.0, self.wind_angle or 1.0)
            self.wind_angle = abs(angle) * sign * DEG
        return self.status()

    @property
    def allure(self) -> str | None:
        """Nom de l'allure visée, si le TWA de consigne en approche une.

        Un angle arbitraire (`/sail?allure=20`) n'en porte aucun : on ne lui en
        invente pas au-delà de `POINT_OF_SAIL_TOLERANCE`.
        """
        angle = abs(self.wind_angle) / DEG
        name = min(POINTS_OF_SAIL, key=lambda n: abs(POINTS_OF_SAIL[n] - angle))
        return name if abs(POINTS_OF_SAIL[name] - angle) <= POINT_OF_SAIL_TOLERANCE else None

    @property
    def amure(self) -> str:
        """Bord d'où vient le vent, d'après le TWA de consigne."""
        return "tribord" if self.wind_angle >= 0.0 else "babord"

    # ------------------------------------------------------------- état -----
    def _common_status(self) -> dict[str, object]:
        """Part de l'état lisible commune à la navigation et au mouillage."""
        return {
            "position": {"lat": round(self.lat, 7), "lon": round(self.lon, 7)},
            "heading_deg": round(self.heading / DEG, 1),
            "cog_deg": round(self.cog / DEG, 1),
            "sog_kn": round(self.sog / KNOT, 2),
            "stw_kn": round(self.stw / KNOT, 2),
            "depth_m": round(self.depth, 2),
            "wind": {
                "tws_kn": round(self.wind_true_speed / KNOT, 1),
                "twa_deg": round(self.wind_true_angle / DEG, 1),
                "aws_kn": round(self.wind_app_speed / KNOT, 1),
                "awa_deg": round(self.wind_app_angle / DEG, 1),
            },
        }

    def status(self) -> dict[str, object]:
        """État lisible de la navigation, pour l'API de contrôle."""
        state = self._common_status()
        state["mode"] = "passage"
        state["target"] = {
            "course_deg": round(self.course / DEG, 1),
            "speed_kn": round(self.speed / KNOT, 2),
            "allure": self.allure,
            "amure": self.amure,
        }
        return state

    # ------------------------------------------------------- évolution ------
    def _wander(self, value: float, target: float, rate: float, dt: float,
                noise: float) -> float:
        """Rappel exponentiel vers `target`, plus un bruit blanc.

        Donne une variable qui dérive doucement sans jamais s'emballer ni se
        figer — le comportement d'un capteur réel filtré.
        """
        value += (target - value) * min(1.0, rate * dt)
        return value + self.rng.gauss(0.0, noise) * dt

    def _wander_angle(self, value: float, target: float, rate: float, dt: float,
                      noise: float, max_rate: float | None = None) -> float:
        """Comme `_wander`, mais par le plus court chemin sur le cercle.

        Sans ce repliement, un cap de 355° visant 5° traverserait tout le tour
        par l'autre bord : le bateau ferait demi-tour au lieu d'abattre de 10°.
        Le cas ne se posait pas tant que le cap visé était une constante ; il
        devient courant dès qu'on le commande depuis l'API.

        `max_rate` (rad/s) borne la giration : un cap commandé de l'autre bord
        fait alors abattre le bateau en une demi-minute, au lieu de faire pivoter
        le compas d'un bloc — et `data/rot` reste une vitesse de rotation tenable.
        """
        delta = _wrap(target - value) * min(1.0, rate * dt)
        if max_rate is not None:
            delta = max(-max_rate * dt, min(max_rate * dt, delta))
        return value + delta + self.rng.gauss(0.0, noise) * dt

    def _tick(self) -> tuple[float, float]:
        """Avance l'horloge et renvoie `(dt borné, phase)`.

        La simulation n'avance que lorsqu'un client RayDB lui demande un lot de
        changements. Sans borne, une pause de dix minutes sans client serait
        rattrapée en un seul pas et téléporterait le bateau ; on préfère qu'il
        reprenne où il en était.
        """
        now = time.monotonic()
        dt = min(MAX_DT, max(1e-3, now - self._last))
        self._last = now
        return dt, now - self.t0

    def step(self) -> dict[str, tuple[int, object]]:
        """Avance la simulation et renvoie les chemins modifiés."""
        dt, phase = self._tick()

        with self._lock:
            course, speed = self.course, self.speed
            wind_speed, wind_angle = self.wind_speed, self.wind_angle

        # Cap : serpente autour de la route commandée, avec une lente oscillation.
        prev_heading = self.heading
        self.heading = self._wander_angle(
            self.heading, course + 6.0 * DEG * math.sin(phase / 47.0),
            0.4, dt, 0.6 * DEG, MAX_TURN_RATE)
        self.heading %= 2 * math.pi

        self.stw = max(0.0, self._wander(self.stw, speed, 0.2, dt, 0.05))
        self.wind_true_speed = max(
            0.0, self._wander(self.wind_true_speed, wind_speed, 0.1, dt, 0.08))
        self.wind_true_angle = _wrap(self._wander_angle(
            self.wind_true_angle, wind_angle, 0.1, dt, 0.4 * DEG))

        # Fond : relief lent + petit bruit de sondeur.
        self.depth = max(1.0, self._wander(
            self.depth, 24.0 + 9.0 * math.sin(phase / 71.0), 0.5, dt, 0.15))

        # Courant de marée : porte le bateau, d'où SOG/COG ≠ STW/cap.
        set_ = 120.0 * DEG + 5.0 * DEG * math.sin(phase / 130.0)
        drift = 0.7 * KNOT
        self.tide_set, self.tide_drift = set_, drift

        # Vecteur fond = vecteur surface + vecteur courant, en repère nord.
        vx = self.stw * math.sin(self.heading) + drift * math.sin(set_)
        vy = self.stw * math.cos(self.heading) + drift * math.cos(set_)
        sog = math.hypot(vx, vy)
        cog = math.atan2(vx, vy) % (2 * math.pi)
        self.sog, self.cog = sog, cog

        # Estime : on intègre la vitesse fond sur dt.
        self.lat += (vy * dt / EARTH_R) / DEG
        self.lon += (vx * dt / (EARTH_R * math.cos(self.lat * DEG))) / DEG

        # Vent apparent = vent réel composé avec la vitesse du bateau.
        self.wind_app_speed, self.wind_app_angle = self._apparent_wind()

        # Houle : roulis et tangage, déphasés, plus une giration résiduelle.
        self.roll = 7.0 * DEG * math.sin(phase / 3.1)
        self.pitch = 2.5 * DEG * math.sin(phase / 4.7 + 1.0)
        self.yaw = 1.5 * DEG * math.sin(phase / 5.3)
        # Giration : déduite du cap réellement parcouru, et filtrée — sur un pas
        # de 0,25 s l'écart brut est surtout du bruit de compas. Elle vaut donc
        # quelques degrés par *minute* quand le bateau tient sa route, et monte
        # franchement quand l'API commande un autre cap. La barre suit.
        self.rot = self._wander(self.rot, _wrap(self.heading - prev_heading) / dt,
                                0.3, dt, 0.0)
        self.rudder = max(-MAX_RUDDER, min(MAX_RUDDER, self.rot * RUDDER_GAIN))
        self.accuracy = 1.8 + 0.3 * math.sin(phase / 11.0)

        self._accumulate()
        return self._publish()

    def _accumulate(self) -> None:
        """Cumuls publiés sous `data/{sog,stw}/{avg,max}` et `data/depth/{min,max}`."""
        self._sog_sum += self.sog
        self._sog_n += 1
        self._sog_max = max(self._sog_max, self.sog)
        self._stw_max = max(self._stw_max, self.stw)
        self._depth_min = min(self._depth_min, self.depth)
        self._depth_max = max(self._depth_max, self.depth)

    def _publish(self) -> dict[str, tuple[int, object]]:
        """Compare l'état à celui du tick précédent et renvoie les écarts."""
        state = self._state()
        changed = {p: v for p, v in state.items() if self._differs(p, v)}
        self._prev = state
        return changed

    def _apparent_wind(self) -> tuple[float, float]:
        """Vent apparent = vent réel composé avec la vitesse du bateau.

        `wind_true_angle` étant relatif à l'étrave, la composition se fait dans
        le repère bateau : l'étrave avance, donc le vent vu recule vers l'avant.
        """
        awx = self.wind_true_speed * math.sin(self.wind_true_angle)
        awy = self.wind_true_speed * math.cos(self.wind_true_angle) + self.stw
        return math.hypot(awx, awy), math.atan2(awx, awy)

    def _state(self) -> dict[str, tuple[int, object]]:
        """Table des chemins `data/…` construite depuis l'état courant.

        Séparée de `step()` pour qu'une sous-classe puisse produire un autre
        mouvement (cf. `AnchorSim`) sans redéfinir la publication.
        """
        return {
            "data/position": (T_STRING, f"{self.lat:.6f},{self.lon:.6f}"),
            "data/position/altitude": (T_FLOAT, 0.4),
            "data/position/accuracy": (T_FLOAT, self.accuracy),
            "data/sog": (T_FLOAT, self.sog),
            "data/sog/avg": (T_FLOAT, self._sog_sum / max(1, self._sog_n)),
            "data/sog/max": (T_FLOAT, self._sog_max),
            "data/cog": (T_FLOAT, self.cog),
            "data/cog/stable": (T_FLOAT, self.cog),
            "data/stw": (T_FLOAT, self.stw),
            "data/stw/avg": (T_FLOAT, self.stw),
            "data/stw/max": (T_FLOAT, self._stw_max),
            "data/heading/true": (T_FLOAT, self.heading),
            "data/heading/magnetic": (T_FLOAT, (self.heading - self.variation) % (2 * math.pi)),
            "data/bearing/variation": (T_FLOAT, self.variation),
            "data/depth": (T_DOUBLE, self.depth),
            "data/depth/offset": (T_DOUBLE, 0.35),
            "data/depth/min": (T_DOUBLE, self._depth_min),
            "data/depth/max": (T_DOUBLE, self._depth_max),
            "data/wind/speed/true": (T_FLOAT, self.wind_true_speed),
            "data/wind/direction/true": (T_FLOAT, self.wind_true_angle),
            "data/wind/speed/apparent": (T_FLOAT, self.wind_app_speed),
            "data/wind/direction/apparent": (T_FLOAT, self.wind_app_angle),
            "data/roll": (T_FLOAT, self.roll),
            "data/pitch": (T_FLOAT, self.pitch),
            "data/yaw": (T_FLOAT, self.yaw),
            "data/rot": (T_FLOAT, self.rot),
            "data/rudder": (T_FLOAT, self.rudder),
            "data/tide/set": (T_FLOAT, self.tide_set),
            "data/tide/drift": (T_FLOAT, self.tide_drift),
        }

    def _differs(self, path: str, value: tuple[int, object]) -> bool:
        """Un chemin n'est repoussé que si sa valeur a bougé de façon notable.

        Le seuil évite d'inonder les clients d'UPDATE pour du bruit de dernier
        bit, tout en laissant passer les vraies variations.
        """
        old = self._prev.get(path)
        if old is None:
            return True
        if isinstance(value[1], str):
            return old[1] != value[1]
        return abs(float(old[1]) - float(value[1])) > 1e-4  # type: ignore[arg-type]

    def snapshot(self) -> dict[str, tuple[int, object]]:
        """État courant complet, pour l'envoi « retained » à la souscription."""
        if not self._prev:
            self.step()
        return dict(self._prev)


class AnchorSim(BoatSim):
    """Bateau au mouillage : évitage autour d'une ancre fixe, puis dérapage.

    L'évitage n'est pas une rotation imposée. Le bateau se met **bout au vent**,
    l'ancre par l'avant : il se place donc *sous le vent* de l'ancre, au bout de
    sa chaîne. C'est la bascule lente de la direction du vent qui le promène
    autour du point de mouillage, et l'embardée (le « pendule » d'un bord sur
    l'autre) qui l'anime. Le rayon oscille entre `swing_min` et `swing_max` au
    rythme de la marée : à longueur de chaîne constante, le bateau s'écarte de
    son ancre quand l'eau baisse et s'en rapproche quand elle monte.

    Cette construction garde les grandeurs cohérentes entre elles — SOG et COG
    sont déduits du déplacement réellement parcouru, et le vent apparent de
    l'angle réel entre l'étrave et le vent. Un client NMEA voit donc un bateau
    qui tourne doucement autour de sa position à quelques dixièmes de nœud.

    `drag()` simule le dérapage : l'ancre décroche, le bateau file en ligne
    droite à la vitesse demandée et s'éloigne au-delà du point fixe.
    `set_anchor()` remouille — ici, ou aux coordonnées données.
    """

    def __init__(self, lat: float = DEFAULT_ANCHOR_LAT, lon: float = DEFAULT_ANCHOR_LON,
                 swing_min: float = DEFAULT_SWING_MIN, swing_max: float = DEFAULT_SWING_MAX,
                 depth: float = DEFAULT_ANCHOR_DEPTH, swing_period: float = DEFAULT_SWING_PERIOD,
                 seed: int | None = None) -> None:
        super().__init__(lat=lat, lon=lon, speed=0.0, seed=seed)
        self.anchor_lat = lat
        self.anchor_lon = lon
        self.swing_min = min(swing_min, swing_max)
        self.swing_max = max(swing_min, swing_max)
        self.swing_period = swing_period
        self.radius = 0.5 * (self.swing_min + self.swing_max)

        # Mouillage : petits fonds, et le sondeur ne verra plus que la marée.
        self.depth = self._depth_base = depth
        self._depth_min = self._depth_max = depth

        # Azimut ancre→bateau : c'est *la* variable du mouvement. Elle tourne
        # lentement (renverse du courant, bascule du vent), et tout en découle —
        # position sur le cercle, direction du vent, cap.
        self.swing_az = self.rng.uniform(0.0, 2 * math.pi)
        self.wind_from = (self.swing_az + math.pi) % (2 * math.pi)
        # Au mouillage, le vent ne suit plus la vitesse du bateau : c'est la
        # brise d'abri, et c'est elle qui oriente l'évitage.
        self.wind_speed = self.wind_true_speed = 11.0 * KNOT
        self.stw = 0.0

        # Le bateau démarre au bout de sa chaîne, pas sur l'ancre : sinon le
        # premier tick le verrait franchir 35 m d'un coup.
        self.lat, self.lon = self._swing_target()

        # Vecteur vitesse filtré, d'où sortent SOG et COG (cf. `step`).
        self._vx = self._vy = 0.0

        # Consigne de dérive, posée par `drag()` depuis le thread de l'API.
        self._drag_course: float | None = None
        self._drag_speed = 0.0

    # -------------------------------------------------------- commandes -----
    @property
    def dragging(self) -> bool:
        return self._drag_speed > 0.0

    def drag(self, course: float | None = None,
             speed: float = DEFAULT_DRAG_SPEED) -> dict[str, object]:
        """Fait déraper le mouillage.

        `course` est un cap vrai en degrés ; omis, il est tiré au hasard —
        « n'importe quelle direction ». `speed` est en nœuds.
        """
        with self._lock:
            self._drag_course = (self.rng.uniform(0.0, 2 * math.pi) if course is None
                                 else (course * DEG) % (2 * math.pi))
            self._drag_speed = max(0.0, speed) * KNOT
        return self.status()

    def set_anchor(self, lat: float | None = None,
                   lon: float | None = None) -> dict[str, object]:
        """Remouille, et arrête une dérive en cours.

        Sans coordonnées, l'ancre est posée **par l'avant**, à une longueur de
        chaîne du bateau : celui-ci ne bouge pas, il est déjà au bout de sa
        chaîne — ce qui se passe quand on mouille et qu'on évite sur son ancre.
        Avec des coordonnées, le bateau rejoint son cercle d'évitage à vitesse
        bornée au lieu de s'y téléporter.
        """
        with self._lock:
            if lat is None or lon is None:
                self.swing_az = (self.heading + math.pi) % (2 * math.pi)
                self.anchor_lat, self.anchor_lon = self._offset(
                    self.lat, self.lon, self.heading, self.radius)
            else:
                self.anchor_lat, self.anchor_lon = float(lat), float(lon)
            self._drag_course = None
            self._drag_speed = 0.0
        return self.status()

    def status(self) -> dict[str, object]:
        """État lisible du mouillage, pour l'API de contrôle."""
        state = self._common_status()
        state["mode"] = "drift" if self.dragging else "anchored"
        state["anchor"] = {"lat": round(self.anchor_lat, 7),
                           "lon": round(self.anchor_lon, 7)}
        state["distance_m"] = round(self.distance_to_anchor(), 1)
        state["swing_radius_m"] = [self.swing_min, self.swing_max]
        state["wind_from_deg"] = round(self.wind_from / DEG, 1)
        state["drift"] = None if not self.dragging else {
            "course_deg": round((self._drag_course or 0.0) / DEG, 1),
            "speed_kn": round(self._drag_speed / KNOT, 2),
        }
        return state

    def distance_to_anchor(self) -> float:
        """Distance bateau→ancre, en mètres (plan tangent, suffisant ici)."""
        dy = (self.lat - self.anchor_lat) * DEG * EARTH_R
        dx = (self.lon - self.anchor_lon) * DEG * EARTH_R * math.cos(self.anchor_lat * DEG)
        return math.hypot(dx, dy)

    def bearing_from_anchor(self) -> float:
        """Azimut ancre→bateau réellement observé, en radians."""
        dy = (self.lat - self.anchor_lat)
        dx = (self.lon - self.anchor_lon) * math.cos(self.anchor_lat * DEG)
        return math.atan2(dx, dy) % (2 * math.pi)

    @staticmethod
    def _offset(lat: float, lon: float, bearing: float, dist: float) -> tuple[float, float]:
        """Point à `dist` mètres dans l'azimut `bearing` depuis (lat, lon)."""
        return (lat + (dist * math.cos(bearing) / EARTH_R) / DEG,
                lon + (dist * math.sin(bearing) / (EARTH_R * math.cos(lat * DEG))) / DEG)

    def _swing_target(self) -> tuple[float, float]:
        """Position visée sur le cercle d'évitage, au bout de la chaîne."""
        return self._offset(self.anchor_lat, self.anchor_lon, self.swing_az, self.radius)

    def _approach(self, lat: float, lon: float, dt: float) -> None:
        """Rejoint la position visée sans jamais dépasser `MAX_SWING_SPEED`.

        En évitage la cible dérive lentement et le bateau colle dessus ; après
        un remouillage ailleurs il s'y rend au lieu d'y sauter, et `data/sog`
        reste une vitesse de bateau au lieu d'un artefact de téléportation.
        """
        dy = (lat - self.lat) * DEG * EARTH_R
        dx = (lon - self.lon) * DEG * EARTH_R * math.cos(self.lat * DEG)
        dist = math.hypot(dx, dy)
        reach = MAX_SWING_SPEED * dt
        if dist <= reach:
            self.lat, self.lon = lat, lon
        else:
            self.lat, self.lon = self._offset(self.lat, self.lon,
                                              math.atan2(dx, dy), reach)

    # --------------------------------------------------------- évolution ----
    def step(self) -> dict[str, tuple[int, object]]:
        """Avance le mouillage et renvoie les chemins modifiés."""
        dt, phase = self._tick()

        with self._lock:
            drag_course, drag_speed = self._drag_course, self._drag_speed

        # Azimut ancre→bateau : un tour complet par `swing_period`, plus un
        # flottement. C'est la renverse du courant qui promène ainsi le bateau
        # tout autour de son ancre.
        self.swing_az = (self.swing_az + 2 * math.pi * dt / self.swing_period
                         + self.rng.gauss(0.0, 0.25 * DEG) * dt) % (2 * math.pi)

        # Le bateau étant sous le vent de son ancre, le vent vient de l'ancre.
        self.wind_from = (self.swing_az + math.pi) % (2 * math.pi)
        self.wind_true_speed = max(
            0.0, self._wander(self.wind_true_speed, self.wind_speed, 0.1, dt, 0.08))

        # Embardée : ±12°, une trentaine de secondes de période. Le bateau fait
        # le pendule au bout de sa chaîne, étrave au vent donc ancre par l'avant.
        sheer = 12.0 * DEG * math.sin(phase / 29.0)
        prev_heading = self.heading
        self.heading = (self.wind_from + sheer) % (2 * math.pi)

        # Rayon d'évitage : c'est la marée qui le fait varier. À longueur de
        # chaîne constante, le bateau s'écarte de son ancre quand l'eau baisse
        # et s'en rapproche quand elle monte — d'où l'opposition de signe avec
        # la sonde plus bas. Suivi même pendant une dérive : figé, il prendrait
        # du retard sur sa consigne et le remouillage se paierait d'un
        # rattrapage brutal.
        tide = math.sin(phase / TIDE_PERIOD)
        mid = 0.5 * (self.swing_min + self.swing_max)
        amp = 0.5 * (self.swing_max - self.swing_min)
        self.radius = min(self.swing_max, max(self.swing_min, self._wander(
            self.radius, mid - amp * tide, 0.4, dt, 0.02)))

        prev_lat, prev_lon = self.lat, self.lon
        if drag_speed > 0.0 and drag_course is not None:
            # Dérapage : l'ancre ne retient plus rien, on file en estime.
            self.lat, self.lon = self._offset(self.lat, self.lon, drag_course,
                                              drag_speed * dt)
        else:
            self._approach(*self._swing_target(), dt)

        # SOG/COG déduits du déplacement réel, mais c'est le **vecteur vitesse**
        # qui est filtré, pas l'angle : sur un pas de 0,25 s le bateau parcourt
        # quelques centimètres, dont la direction n'est que du bruit. Lisser le
        # cap après coup ne servirait à rien — il faut moyenner avant de le
        # calculer. C'est aussi ce que fait un GPS.
        vx = (self.lon - prev_lon) * DEG * EARTH_R * math.cos(self.lat * DEG) / dt
        vy = (self.lat - prev_lat) * DEG * EARTH_R / dt
        self._vx = self._wander(self._vx, vx, COG_FILTER, dt, 0.0)
        self._vy = self._wander(self._vy, vy, COG_FILTER, dt, 0.0)

        self.sog = math.hypot(self._vx, self._vy)
        # Sous une vitesse plancher, la route n'a plus de sens : on garde la
        # dernière connue, comme un GPS qui fige son COG à l'arrêt.
        if self.sog > COG_FLOOR:
            self.cog = math.atan2(self._vx, self._vy) % (2 * math.pi)
        self.stw = self.sog     # la coque traverse l'eau à la même vitesse

        # L'étrave est face au vent : l'angle relatif vaut l'embardée près.
        self.wind_true_angle = _wrap(self.wind_from - self.heading)
        self.wind_app_speed, self.wind_app_angle = self._apparent_wind()

        # Au mouillage, le sondeur ne voit plus que la marée et le clapot.
        self.depth = max(1.0, self._wander(
            self.depth, self._depth_base + 1.2 * tide, 0.3, dt, 0.05))

        # Mouvements : clapot d'abri, et une giration qui est celle du cap.
        self.roll = 3.0 * DEG * math.sin(phase / 4.3)
        self.pitch = 1.2 * DEG * math.sin(phase / 3.7 + 0.6)
        self.yaw = sheer
        self.rot = _wrap(self.heading - prev_heading) / dt
        self.rudder = 0.0       # barre libre, moteur stoppé
        self.accuracy = 1.6 + 0.25 * math.sin(phase / 13.0)

        # Courant de marée résiduel dans l'abri.
        self.tide_set = (60.0 * DEG + 8.0 * DEG * math.sin(phase / 150.0)) % (2 * math.pi)
        self.tide_drift = 0.2 * KNOT

        self._accumulate()
        return self._publish()


class Simulation:
    """Le bateau du moment, et le passage à chaud d'un mode à l'autre.

    `--anchor` fixait le mode au lancement : mouiller en cours de route
    demandait un redémarrage. Cette façade porte le bateau courant et le
    remplace sur commande — `anchor()` mouille, `underway()` relève l'ancre.

    RayDB ne voit qu'un objet stable dont `step()` et `snapshot()` suivent le
    bateau en cours. Au changement, le nouveau bateau republie tout son état
    d'un bloc (`_adopt`) : les clients reçoivent le lot d'UPDATE qu'ils
    reçoivent déjà à la souscription, plutôt qu'un état à moitié périmé.

    Le bateau de navigation est **conservé** pendant le mouillage au lieu d'être
    reconstruit : appareiller reprend ses consignes de vitesse et d'allure sans
    que l'API ait à les redonner. Le cap, lui, repart de celui où l'ancre tenait
    le bateau — on quitte un mouillage sur l'étrave qu'on a, pas sur celle qu'on
    avait en arrivant.
    """

    def __init__(self, boat: BoatSim,
                 swing: tuple[float, float] = (DEFAULT_SWING_MIN, DEFAULT_SWING_MAX),
                 depth: float = DEFAULT_ANCHOR_DEPTH) -> None:
        self.boat = boat
        self.swing_min, self.swing_max = swing
        self.anchor_depth = depth
        # Bateau de navigation mis de côté le temps d'un mouillage.
        self._passage = boat if not isinstance(boat, AnchorSim) else None

    # --------------------------------------- ce que RayDB attend d'un bateau --
    def step(self) -> dict[str, tuple[int, object]]:
        return self.boat.step()

    def snapshot(self) -> dict[str, tuple[int, object]]:
        return self.boat.snapshot()

    def status(self) -> dict[str, object]:
        return self.boat.status()

    @property
    def anchored(self) -> bool:
        return isinstance(self.boat, AnchorSim)

    # ---------------------------------------------------- changement de mode --
    def _adopt(self, boat: BoatSim) -> None:
        """Passe la main à `boat`, en forçant la republication de tout l'état.

        Le client a vu l'autre bateau entre-temps : un chemin revenu à sa
        valeur d'avant serait tenu pour inchangé et resterait périmé chez lui.
        L'horloge est reprise à zéro pour que le premier pas ne rattrape pas le
        temps passé de côté.
        """
        boat._prev = {}
        boat._last = time.monotonic()
        self.boat = boat

    def anchor(self, lat: float | None = None,
               lon: float | None = None) -> dict[str, object]:
        """Mouille — ou remouille, si le bateau l'était déjà.

        Sans coordonnées, l'ancre tombe **par l'avant** du bateau, qui reste où
        il est. Avec, le bateau prend son mouillage là-bas : en navigation il
        s'y rend d'un bond — c'est un changement de décor, pas une manœuvre —,
        déjà au mouillage il rejoint le nouveau cercle d'évitage à vitesse
        bornée, comportement d'origine de `AnchorSim.set_anchor`.
        """
        if self.anchored:
            return self.boat.set_anchor(lat, lon)   # type: ignore[attr-defined]

        passage = self._passage = self.boat
        if lat is None or lon is None:
            lat, lon = passage.lat, passage.lon
        else:
            lat, lon = check_position(lat, lon)
        anchored = AnchorSim(lat=lat, lon=lon, swing_min=self.swing_min,
                             swing_max=self.swing_max, depth=self.anchor_depth)
        if (lat, lon) == (passage.lat, passage.lon):
            # Mouillage sur place : le bateau ne bouge pas, il évitera autour de
            # l'ancre que `set_anchor()` pose une longueur de chaîne devant lui.
            anchored.lat, anchored.lon = passage.lat, passage.lon
            anchored.heading = passage.heading
            self._adopt(anchored)
            return anchored.set_anchor()
        self._adopt(anchored)
        return anchored.status()

    def underway(self, heading: float | None = None, speed: float | None = None,
                 allure: object = None, amure: object = None) -> dict[str, object]:
        """Appareille, et pose au passage les consignes données.

        Le bateau repart d'où il mouillait, sur le cap où l'ancre le tenait et
        à sa vitesse du moment — soit à peu près l'arrêt : il accélère au lieu
        de bondir. Déjà en route, la commande se réduit aux consignes, ce qui
        permet à un script de tout poser d'un appel.
        """
        if self.anchored:
            anchored = self.boat
            boat = self._passage or BoatSim(lat=anchored.lat, lon=anchored.lon)
            boat.lat, boat.lon = anchored.lat, anchored.lon
            boat.course = boat.heading = anchored.heading
            boat.stw = anchored.stw
            boat.depth = anchored.depth     # le fond remonte, il ne saute pas
            self._adopt(boat)

        if heading is not None:
            self.boat.set_heading(heading)
        if speed is not None:
            self.boat.set_speed(speed)
        if allure is not None or amure is not None:
            self.boat.set_sail(allure, amure)
        return self.boat.status()
