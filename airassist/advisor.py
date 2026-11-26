"""Rule-based planner: household profile + air state -> ranked, quantified actions.

Every number users see comes from the small exposure model below, so advice is
explainable and testable. The GenAI layer (llm.py) only rephrases this output.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace

from .aqi import AirState

# Exposure model. These constants are illustrative assumptions, shown to users.
INDOOR_FACTOR = 0.6  # indoor/outdoor PM2.5 ratio in a naturally ventilated home
SOURCE_BOOST = 0.3  # added by biomass cooking, and again by biomass/coal heating
PURIFIER_CUT = 0.5  # indoor particles removed by a right-sized HEPA or DIY box filter
SEALING_CUT = 0.2  # further indoor reduction from shut windows and sealed gaps
MASK_CUT = 0.8  # outdoor inhalation removed by a well-fitted N95/FFP2
MIDDAY_CUT = 0.25  # winter midday air is about 25% cleaner than dawn and dusk air

ASSUMPTIONS = (
    "These are illustrative estimates, not measurements or medical advice.",
    f"Indoor air starts at {INDOOR_FACTOR:.0%} of outdoor levels; biomass cooking or "
    f"heating each add {SOURCE_BOOST:.0%}.",
    f"A right-sized purifier removes about {PURIFIER_CUT:.0%} of indoor particles; "
    f"sealing gaps about {SEALING_CUT:.0%} more.",
    f"A well-fitted N95 removes about {MASK_CUT:.0%} outdoors; midday trips meet "
    f"about {MIDDAY_CUT:.0%} less pollution.",
    "Percentages per step overlap, so they do not add up to the combined figure.",
)

COMMUTES = ("none", "walk_cycle", "two_wheeler", "car", "public_transport")
COOKING = ("lpg_png", "electric", "biomass")
HEATING = ("none", "electric", "biomass_coal")
LANGUAGES = {
    "en": "English", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
    "mr": "Marathi", "ur": "Urdu", "es": "Spanish", "zh": "Chinese",
}  # fmt: skip
_CHOICES = {
    "commute": COMMUTES, "cooking": COOKING, "heating": HEATING, "language": tuple(LANGUAGES),
}  # fmt: skip
MAX_OUTDOOR_HOURS = 16


@dataclass(frozen=True)
class Profile:
    """Household and lifestyle facts that tailor the plan."""

    children: bool = False
    elderly: bool = False
    pregnancy: bool = False
    heart_lung: bool = False  # asthma, COPD, heart disease
    has_purifier: bool = False
    commute: str = "none"
    cooking: str = "lpg_png"
    heating: str = "none"
    outdoor_hours: float = 2.0
    language: str = "en"

    @property
    def sensitive(self) -> bool:
        return self.children or self.elderly or self.pregnancy or self.heart_lung

    @classmethod
    def from_dict(cls, data: object) -> Profile:
        """Strictly validate untrusted input: unknown keys and wrong types are rejected."""
        if not isinstance(data, Mapping):
            raise ValueError("profile must be an object")
        if not set(data) <= {f.name for f in fields(cls)}:
            raise ValueError("profile has unknown fields")
        defaults = cls()
        for name, value in data.items():
            default = getattr(defaults, name)
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif name in _CHOICES:
                ok = isinstance(value, str) and value in _CHOICES[name]
            else:  # outdoor_hours
                ok = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and 0 <= value <= MAX_OUTDOOR_HOURS
                )
            if not ok:
                raise ValueError(f"invalid value for {name}")
        return cls(**data)


@dataclass(frozen=True)
class Scenario:
    """Inputs to the exposure model for one household on one day."""

    indoor_factor: float
    outdoor_hours: float
    outdoor_scale: float = 1.0  # < 1 when outings move to cleaner hours
    mask_cut: float = 0.0

    def exposure(self, pm25: float) -> float:
        """Time-weighted average PM2.5 breathed over 24 hours, in µg/m³."""
        outdoor = self.outdoor_hours * self.outdoor_scale * (1 - self.mask_cut)
        indoor = (24 - self.outdoor_hours) * self.indoor_factor
        return pm25 * (outdoor + indoor) / 24


def baseline(profile: Profile) -> Scenario:
    boost = SOURCE_BOOST * ((profile.cooking == "biomass") + (profile.heating == "biomass_coal"))
    return Scenario(indoor_factor=INDOOR_FACTOR + boost, outdoor_hours=profile.outdoor_hours)


def _indoor(cut: float) -> Callable[[Scenario], Scenario]:
    return lambda s: replace(s, indoor_factor=s.indoor_factor * (1 - cut))


def _less_source(s: Scenario) -> Scenario:
    return replace(s, indoor_factor=s.indoor_factor - SOURCE_BOOST)


def _midday(s: Scenario) -> Scenario:
    return replace(s, outdoor_scale=s.outdoor_scale * (1 - MIDDAY_CUT))


def _mask(s: Scenario) -> Scenario:
    return replace(s, mask_cut=MASK_CUT)


@dataclass(frozen=True)
class Action:
    id: str
    category: str  # health | routine | indoor | sustainable
    title: str
    why: str  # the mechanism, in plain language
    applies: Callable[[Profile, AirState], bool]
    effect: Callable[[Scenario], Scenario] | None = None  # None: not quantified
    community: bool = False  # benefits neighbours, not just this household


def _always(p: Profile, a: AirState) -> bool:
    return True


def _high(a: AirState) -> bool:  # unhealthy for sensitive groups or worse
    return a.level >= 2


def _severe(a: AirState) -> bool:  # unhealthy or worse
    return a.level >= 3


# Order matters: source removal comes first so later percentage cuts apply to what remains.
ACTIONS: tuple[Action, ...] = (
    Action(
        "clean_cooking", "sustainable", "Move cooking off wood, dung and coal",
        "Switch to LPG, piped gas or induction. Until then, cook outdoors or under a chimney "
        "and keep young children out of the kitchen. Solid-fuel smoke is often the biggest "
        "indoor source of fine particles.",
        applies=lambda p, a: p.cooking == "biomass", effect=_less_source,
    ),
    Action(
        "clean_heating", "sustainable", "Heat with insulation and electric heaters, not open fires",
        "Burning coal, wood or rubbish indoors fills rooms with fine particles and carbon "
        "monoxide, so never run a brazier in a closed room. Layers, blankets, hot-water "
        "bottles and draught-proofing keep you warm without the smoke.",
        applies=lambda p, a: p.heating == "biomass_coal", effect=_less_source,
    ),
    Action(
        "purifier_run", "indoor", "Run your air purifier in the room where you spend most time",
        "Keep the door shut, start on high for an hour, then switch to auto. Replace filters "
        "on schedule, because a clogged filter cleans very little.",
        applies=lambda p, a: p.has_purifier and _high(a), effect=_indoor(PURIFIER_CUT),
    ),
    Action(
        "purifier_diy", "indoor", "Build a low-cost box-fan air cleaner",
        "Tape four MERV-13 furnace filters into a cube and fix a recent box fan on top (a "
        "Corsi-Rosenthal box). Tests show it can clean a room's air about as well as many "
        "HEPA purifiers for a fraction of the price.",
        applies=lambda p, a: not p.has_purifier and _high(a), effect=_indoor(PURIFIER_CUT),
    ),
    Action(
        "seal_home", "indoor", "Keep windows shut at dawn and dusk and seal draughts",
        "Smoke and exhaust pool near the ground when winter air is cold and still. Shut "
        "windows, weather-stripping and a rolled towel under doors let much less of it in.",
        applies=lambda p, a: _severe(a), effect=_indoor(SEALING_CUT),
    ),
    Action(
        "ventilate", "indoor", "Air the home for 10 to 15 minutes around midday",
        "Midday is usually the cleanest part of a winter day. Open windows on opposite sides "
        "briefly, then close them again.",
        applies=lambda p, a: a.level <= 2,
    ),
    Action(
        "kitchen_vent", "indoor", "Use the exhaust fan or chimney while cooking, and for 15 minutes after",
        "Frying and high-heat cooking release fine particles even on clean fuels, and a fan "
        "sends them outside instead of into the room.",
        applies=lambda p, a: p.cooking != "biomass",
    ),
    Action(
        "wet_clean", "indoor", "Damp-mop and wipe instead of sweeping or dry dusting",
        "Dry sweeping throws settled dust back into the air you breathe, while a damp cloth "
        "traps it.",
        applies=_always,
    ),
    Action(
        "no_gimmicks", "indoor", "Skip incense, candles, sprays, ozone generators and ionisers",
        "They add particles or ozone to indoor air. Houseplants and salt lamps do not clean "
        "air in any measurable way.",
        applies=lambda p, a: _high(a),
    ),
    Action(
        "midday_outdoors", "routine", "Do outdoor errands, walks and school runs at midday (about 11:00 to 16:00)",
        "Cold winter air traps pollution near the ground at dawn and after dark. Sun-warmed "
        "midday air mixes and thins it out.",
        applies=lambda p, a: _high(a) and p.outdoor_hours > 0, effect=_midday,
    ),
    Action(
        "mask", "routine", "Wear a well-fitted N95 or FFP2 respirator outdoors",
        "Cloth and surgical masks block far less fine dust than a respirator that seals "
        "around the nose and chin. Not for children under 2, or anyone who struggles to "
        "breathe through one.",
        applies=lambda p, a: p.outdoor_hours > 0
        and (_severe(a) or (_high(a) and (p.sensitive or p.commute in ("walk_cycle", "two_wheeler")))),
        effect=_mask,
    ),
    Action(
        "exercise_indoors", "routine", "Move hard exercise indoors and keep outdoor activity light",
        "Heavy breathing can multiply the amount of particles you take in several times over.",
        applies=lambda p, a: _severe(a),
    ),
    Action(
        "car_cabin", "routine", "In the car, keep windows up and use recirculated air",
        "This stops the cabin drawing in the exhaust of the vehicles around you. Replace the "
        "cabin filter on schedule.",
        applies=lambda p, a: p.commute == "car" and _high(a),
    ),
    Action(
        "check_aqi", "routine", "Check the air reading every morning and evening",
        "It can swing within hours, so plan school runs, exercise and airing the house "
        "around the colour band.",
        applies=_always,
    ),
    Action(
        "sensitive_care", "health", "Keep children, older adults and anyone with asthma or heart disease protected",
        "On bad days keep them indoors with clean air, keep prescribed inhalers and medicines "
        "within reach, and ask a doctor for a written action plan. Get medical help promptly "
        "for breathlessness, chest pain, wheezing or a cough that will not settle.",
        applies=lambda p, a: p.sensitive and _high(a),
    ),
    Action(
        "commute_swap", "sustainable", "Swap solo car and two-wheeler trips for bus, metro, carpool or walking",
        "Road transport is a leading source of nitrogen oxides and CO2 and a significant "
        "source of fine particles, so each trip avoided cleans your own street.",
        applies=lambda p, a: p.commute in ("car", "two_wheeler"), community=True,
    ),
    Action(
        "no_burning", "sustainable", "Never burn leaves, rubbish or plastic, and report open burning",
        "Open burning is a major, avoidable contributor to winter smog in many cities. "
        "Compost garden waste, use municipal collection and report fires to your local authority.",
        applies=_always, community=True,
    ),
    Action(
        "community", "sustainable", "Agree a street-level plan with neighbours, your resident association or school",
        "Share cars, keep diesel generators off, damp down construction dust and move school "
        "sport indoors on bad days. Local sources are the ones a community can actually cut.",
        applies=lambda p, a: _high(a), community=True,
    ),
)  # fmt: skip


def _drop(before: float, after: float) -> int:
    return round(100 * (before - after) / before) if before > 0 else 0


def build_plan(profile: Profile, air: AirState) -> dict:
    """Return a JSON-ready plan: air summary, exposure before/after, ranked actions."""
    base = baseline(profile)
    before = base.exposure(air.pm25)
    combined, actions = base, []
    for act in ACTIONS:
        if not act.applies(profile, air):
            continue
        reduction = None
        if act.effect:
            combined = act.effect(combined)
            reduction = _drop(before, act.effect(base).exposure(air.pm25))
        actions.append({
            "id": act.id, "category": act.category, "title": act.title, "why": act.why,
            "reduction_pct": reduction, "community": act.community,
        })  # fmt: skip
    actions.sort(key=lambda a: -(a["reduction_pct"] or 0))  # stable: ties keep table order
    after = combined.exposure(air.pm25)
    return {
        "air": air.to_dict(),
        "headline": (
            f"{air.label}: PM2.5 about {air.pm25:g} µg/m³, "
            f"{air.who_multiple:g} times the WHO daily guideline"
        ),
        "exposure": {
            "before": round(before, 1), "after": round(after, 1), "reduction_pct": _drop(before, after),
        },  # fmt: skip
        "actions": actions,
        "assumptions": list(ASSUMPTIONS),
    }
