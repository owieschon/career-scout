"""Whimsical activity-aware progress phrases for the Telegram bot.

Voice direction:

  Every phrase = (a) emoji prefix + (b) ongoing-action language. No idle
  states ("got it"), no reactions ("huh"), no composure ("this is fine"),
  no observational musing ("thinking about how i don't have hands"). The
  message must always read as "Alice is doing something right now."

  Voice: fiddly tactile mumble + mental-work narration + investigative
  surprise + absurd discovery. A competent person fiddling with their work,
  not clueless/searching/lost.

The pools are deliberately large and the recent-phrase tracking persists
across turns within the same daemon process — small pools plus a per-turn
reset would surface the same phrases repeatedly across messages. There are
~250+ unique phrases and a recent-window of 30 (roughly 6 turns at 5 phrases
per turn); the recent window is NOT cleared on reset(), only last_tool and
iter are.
"""
from __future__ import annotations

import random
from threading import Lock


# ─── filler pool ─────────────────────────────────────────────────────────────

# Fiddly tactile — the primary voice. Mid-task hands-on mumble, e.g.
# "just gotta finagle this a little bit to the left".
_FIDDLY = [
    "✋ just gotta finagle this a little bit to the left",
    "🤏 nudging this over",
    "🛠 shimming this up",
    "🪛 torquing it just so",
    "🎯 teasing it out",
    "💪 putting some weight behind this",
    "🤲 working gentle on this part",
    "👀 eyeballing this",
    "↖️ shifting a hair to the left",
    "🔄 cranking through the turns",
    "🔧 working the wrist on this",
    "🎨 finessing this corner",
    "🤝 convincing this to cooperate",
    "🧩 slotting this in",
    "🪡 threading this through",
    "⚖️ leveling this out",
    "🪢 untying this knot",
    "📐 squaring this up",
    "✂️ trimming the edges",
    "🧪 mixing these together",
    "🔌 plugging this in",
    "🚧 routing around this",
    "🛠 fitting this piece in",
    "🦾 muscling through this part",
    "🎢 working through the curves",
    "🪜 climbing into this",
    "🔩 jiggling the handle loose",
    "🤺 jousting with this section",
    "🪟 polishing this up",
    "👂 holding my tongue right",
    "🥄 stirring this part",
    "🎵 finding the rhythm on this",
    "🪙 flipping this around",
    "🏗 building the scaffolding here",
    "🪞 looking at the underside",
    "🧶 unraveling this",
    "🔭 pulling back to see the whole",
    "🔬 zooming in on this detail",
    "🪛 backing this screw out a quarter turn",
    "🔧 loosening this nut a touch",
    "🧤 gloves on for this part",
    "🪨 settling this into place",
    "🥢 picking at this with chopstick precision",
    "🪢 weaving these strands together",
    "🎣 reeling this in slowly",
    "🔪 separating the layers here",
    "🪒 shaving this down a touch",
    "🛞 spinning the dial",
    "🪂 lowering this gently",
    "🏹 drawing the bow on this",
    "🛼 rolling this into position",
    "🦯 feeling my way through this part",
    "🧵 looping this through",
    "🛟 buoying this section",
    "🏔 working up the slope of this",
    "🎨 dabbing this in",
    "🪞 angling the mirror",
    "🍳 flipping this over",
    "🥖 kneading this section",
    "🪡 stitching this up",
    "🪛 quarter-turn at a time",
    "🪚 sawing through this bit",
    "🧹 sweeping the burrs off",
    "🛠 reseating this piece",
    "🪡 hemming the edges",
    "🪤 disarming this part",
    "🛞 fine-tuning the wheel",
    "🪛 micro-adjusting the screw",
    "🪡 darning this hole",
    "🪤 setting the trigger just so",
    "🪟 buffing this clean",
    "🪜 stepping up to the next bit",
    "🪞 catching the reflection to align this",
    "🪜 hopping to the next rung",
    "🎺 puffing some air through this",
    "🧙 willing this into place",
    "🪤 testing the pressure here",
    "🪛 cinching this down",
    "🔧 jimmying this open",
    "🪡 basting this together",
    "🛠 wedging this into place",
    "🔩 backing out this fastener",
    "🪡 lacing this up",
    "🪛 turning, turning",
    "🛠 tapping this home",
]

# Mental work — narration of thinking AS A PROCESS, not as a state.
_MENTAL = [
    "🧠 cross-checking this in my head",
    "💭 turning this over",
    "🎲 weighing this against that",
    "⚖️ comparing the options",
    "🗺 mapping this out",
    "🧮 doing the math on this",
    "🔄 running through it again",
    "🪜 walking through this step by step",
    "🪡 connecting the threads",
    "🧩 fitting the pieces together",
    "📚 paging through what i know",
    "🔭 looking at the bigger picture",
    "🎯 narrowing in on the answer",
    "🪂 dropping into the details",
    "🌊 riding through this thought",
    "🎢 following this train of thought",
    "🪢 tracing this thread back",
    "🧠 connecting these dots",
    "🪞 reflecting this against what i know",
    "🧩 fitting this into the larger picture",
    "🔄 looping back through this",
    "🧠 wiring this up in my head",
    "💭 chewing on this",
    "🪜 reasoning step by step through this",
    "🔍 magnifying this part of the problem",
    "🌊 wave-by-wave through this",
    "📚 paging through what i remember",
    "🧠 simulating this forward",
    "⏳ letting this settle in my head",
    "🪞 holding this up to the light",
    "🎬 playing this scenario forward",
    "🎯 honing in on the crux",
    "🧮 tallying up the implications",
    "📐 measuring this thought against that",
    "🪡 threading the logic together",
    "🔭 zooming out on this",
    "🌀 spiraling in on the right answer",
    "🪜 climbing the logic of this",
    "🧠 running this through once more",
    "💭 sitting with this for a beat",
    "🎯 lining up the shot",
    "🧮 cross-referencing this in my head",
]

# Investigative surprise — "this is unexpected, working through it"
# (NOT pure reaction; she's actively dealing with the surprise)
_INVESTIGATE = [
    "🔍 checking out a weird thing",
    "🤨 investigating this anomaly",
    "👀 looking into something that wasn't on the spec",
    "🪤 untangling this surprise",
    "🛟 navigating around this curveball",
    "🚨 working through a small situation",
    "🫥 figuring out what this is",
    "🧪 testing whether this is what i think it is",
    "🔦 shining a light on this oddity",
    "🔬 examining this thing closer",
    "🧐 inspecting the edges of this",
    "🪤 catching this off-guard",
    "🕵 sleuthing through this",
    "🧪 running a quick experiment on this",
    "📡 picking up signal on this",
    "🔭 scoping this out",
    "🪞 holding this up for inspection",
    "🧲 pulling the answer out of this",
    "🕳 spelunking this rabbit hole",
    "🌡 taking the temperature of this",
    "🩺 diagnosing this part",
    "🔍 retracing the path here",
    "🔦 lighting up this corner",
    "🧪 stress-testing this",
]

# Absurd discovery — odd physical findings, framed as ongoing work.
_ABSURD = [
    "🫘 pulling the beans out of the computer",
    "🩹 patching over some duct tape someone left",
    "🧱 working around a brick someone wedged in here",
    "🕷 dealing with a spider situation",
    "🩼 propping this up with whatever's handy",
    "📎 unbending a paperclip out of the gears",
    "🧴 wiping mystery glue off this",
    "🦴 fishing a chicken bone out of the wiring",
    "🧀 scraping cheese off this",
    "🪙 dislodging a quarter that rolled in here",
    "🦗 evicting a cricket from the works",
    "🍯 scrubbing honey out of the mechanism",
    "🧦 fishing a sock out of the path",
    "🎈 deflating this for some reason",
    "🪦 sweeping cobwebs out of here",
    "🍝 untangling spaghetti wiring",
    "🦷 prying a baby tooth out of here",
    "🪡 freeing a hairpin from the gears",
    "🧂 brushing salt off the contacts",
    "🍪 picking crumbs out of this",
    "🎲 prying a stuck die out of the slot",
    "🥨 unwedging a pretzel from in here",
    "🎮 untangling a controller cord from this",
    "🪥 working a toothbrush out of the works",
]


# Combined filler — NO multiplication. Equal weight per pool, achieved
# through pool sizes rather than weighting. Fiddly is largest by count
# because that's the primary voice, but every category gets meaningful
# rotation.
_FILLER: list[str] = _FIDDLY + _MENTAL + _INVESTIGATE + _ABSURD


# ─── per-tool pools — ongoing form, with emoji ──────────────────────────────

_TOOL_PHRASES: dict[str, list[str]] = {
    "read_sheet": [
        "📊 still digesting what the sheet said",
        "📑 still going through column G",
        "👁 still scanning the rows",
        "🔍 cross-referencing the sheet",
        "📋 working through line by line",
        "🧮 tallying what's in the sheet",
        "📜 still reading down the sheet",
        "🗂 sorting through the sheet",
        "📊 chewing on what the sheet shows",
        "🔢 running the sheet numbers",
        "📋 indexing the rows in my head",
        "📑 still flipping through the sheet",
        "🪡 threading the sheet data together",
        "📊 still paging through the sheet",
        "🔎 hunting for what i need in the sheet",
    ],
    "read_file": [
        "📂 working through the file",
        "📖 reading deeper into this",
        "🔍 picking through the file",
        "📃 going line by line",
        "🪡 stitching the file's bits together",
        "📖 still parsing this file",
        "📄 unfolding the file's logic",
        "📜 reading this top-to-bottom",
        "🔎 looking for the relevant part",
        "📁 still cracking open this file",
        "📖 still chewing through the file",
        "🪚 carving through the file",
    ],
    "read_pending_state": [
        "📋 walking through what's pending",
        "🗂 sifting through the queue",
        "📌 untangling the queue",
        "📥 still going through the queue",
        "📋 paging through the pending list",
        "🗃 cataloging what's pending",
    ],
    "read_focus_state": [
        "🎯 mapping out the focus list",
        "📌 working through what's in focus",
        "🔍 inspecting the focus stack",
        "🎯 still walking the focus list",
        "📋 cataloging the focus items",
        "🗂 sorting through focus",
    ],
    "read_alice_brief": [
        "📜 still reading my own brief",
        "🪞 grounding myself in the brief",
        "🧠 rereading the doctrine",
        "📖 still working through the brief",
        "🪡 stitching the brief to the moment",
    ],
    "read_knowledge_file": [
        "📚 working through the knowledge file",
        "🔍 pulling threads from this knowledge",
        "🧠 absorbing what's in here",
        "📖 still chewing on this knowledge",
        "🪡 weaving this knowledge in",
    ],
    "list_dir": [
        "📁 prowling through the directory",
        "🗄 taking inventory of files",
        "🔎 scanning the filesystem",
        "📂 still walking the directory",
        "🪜 climbing through the file tree",
    ],
    "list_knowledge_files": [
        "📑 walking through what i know",
        "🗃 sifting the knowledge pile",
        "📚 cataloging the knowledge files",
    ],
    "list_pending_experience_candidates": [
        "📋 working through what's pending review",
        "🗂 sifting the candidates pile",
        "📑 cataloging the pending candidates",
    ],
    "enqueue_prep": [
        "📌 still pinning this to the prep board",
        "📋 adding more to the pile",
        "🗃 filing this in",
        "📥 stacking more into the queue",
        "📌 still finishing the pin job",
        "🗂 slotting this into the prep list",
    ],
    "dequeue_prep": [
        "📤 pulling this off the prep stack",
        "✂️ trimming the stack down",
        "🗑 still clearing this from prep",
    ],
    "set_focus": [
        "🎯 locking the focus in",
        "📌 setting this as the focal point",
        "🧲 pulling focus onto this",
    ],
    "add_focus": [
        "🎯 sliding this onto the focus list",
        "📌 stacking this into focus",
        "📥 still adding this to focus",
    ],
    "drop_focus": [
        "🗑 sliding this off the focus list",
        "✂️ trimming this from focus",
        "📤 still pulling this out of focus",
    ],
    "mark_role_status": [
        "✅ wrapping up the status change",
        "📝 still updating the role status",
        "🪧 moving the status pin",
        "📋 still adjusting the status entry",
    ],
    "append_observation": [
        "🗒 jotting this down in the log",
        "📝 still logging this observation",
        "🪶 inking this into the log",
        "📋 still committing this to the log",
    ],
    "flag_experience_candidate": [
        "🚩 still raising the flag on this",
        "📌 pinning the flag in place",
        "🏷 still tagging this entry",
    ],
    "generate_application_package": [
        "📦 still assembling the package",
        "✍️ stitching the pieces together",
        "🧩 fitting the materials in",
        "🪡 sewing the package together",
        "🏗 building the package up",
        "🎨 polishing the application",
        "📦 still wrapping the package",
        "📝 finishing the cover draft",
        "📨 prepping the package for sending",
        "🪡 threading the resume together",
        "🏗 raising the package frame",
        "🎁 wrapping this with a bow",
        "📦 still cinching the package shut",
        "✍️ buffing the cover letter prose",
    ],
    "describe_capabilities": [
        "🛠 walking through my capabilities",
        "📋 going through what i can do",
        "🧰 laying out the toolkit",
        "📜 still cataloging my abilities",
    ],
    "write_file": [
        "💾 still saving this down",
        "✍️ committing more to disk",
        "📝 writing the rest",
        "🪶 inking this in",
        "💾 still serializing",
        "📥 still flushing to disk",
        "🖊 still drafting this onto the page",
    ],
    "ask_confirmation": [
        "🤔 composing the question",
        "✍️ drafting what to ask you",
        "🎤 figuring out how to phrase this",
    ],
}


# ─── state + selection ──────────────────────────────────────────────────────

_lock = Lock()
_state: dict = {
    "last_tool": None,
 # recent_phrases persists ACROSS turns within a daemon process —
 # only cleared on daemon restart. This keeps phrase variety across
 # consecutive messages.
    "recent_phrases": [],
    "iter": 0,
}

# Window of phrases to avoid re-picking. Larger window = more variety
# across turns. 30 ≈ 6 turns at 5 picks per turn before a phrase becomes
# eligible again.
_RECENT_WINDOW = 30

# Probability of pulling from the activity pool when a tool has fired.
# Kept at 0.50 so the tool pool doesn't dominate a turn and crowd out filler
# variety.
_ACTIVITY_BIAS = 0.50


def reset() -> None:
    """Called at the start of a chat turn. Clears per-turn state
    (last_tool, iter) but PRESERVES recent_phrases so the no-repeat
    window crosses turn boundaries. Without this, turn 2 can re-pick what
    turn 1 just used and the same phrases repeat across messages."""
    with _lock:
        _state["last_tool"] = None
        _state["iter"] = 0
 # recent_phrases intentionally NOT cleared


def record_tool(name: str) -> None:
    """Called by llm.py's tool loop after each successful tool. The next
    whimsical edit will preferentially pull from this tool's phrase
    pool."""
    with _lock:
        _state["last_tool"] = name


def next_phrase() -> str:
    """Pick the next progress-edit phrase. Activity-aware if a tool fired
    recently (50% probability), otherwise filler. Avoids the last
    _RECENT_WINDOW phrases used so rotation feels varied even across
    multiple turns within the same daemon process."""
    with _lock:
        _state["iter"] += 1
        last_tool = _state["last_tool"]
        pool = _FILLER
        if last_tool and last_tool in _TOOL_PHRASES:
            if random.random() < _ACTIVITY_BIAS:
                pool = _TOOL_PHRASES[last_tool]

 # Avoid anything in the recent window
        recent = set(_state["recent_phrases"][-_RECENT_WINDOW:])
        candidates = [p for p in pool if p not in recent]
 # If the pool is too small for the window, fall back to all-pool
 # (still randomized, just can't avoid the recent set entirely)
        if not candidates:
            candidates = pool
        phrase = random.choice(candidates)
        _state["recent_phrases"].append(phrase)
 # Keep the recent buffer bounded so it doesn't grow unboundedly
 # across long daemon runs
        max_recent = _RECENT_WINDOW * 4
        if len(_state["recent_phrases"]) > max_recent:
            _state["recent_phrases"] = _state["recent_phrases"][-max_recent:]
        return phrase


def pool_size_report() -> dict:
    """For diagnostic / log purposes — exposes pool sizes so we can
    confirm in startup logs how much variety is loaded."""
    return {
        "filler_total": len(_FILLER),
        "filler_unique": len(set(_FILLER)),
        "fiddly": len(_FIDDLY),
        "mental": len(_MENTAL),
        "investigate": len(_INVESTIGATE),
        "absurd": len(_ABSURD),
        "tool_phrase_total": sum(len(v) for v in _TOOL_PHRASES.values()),
        "tools_with_phrases": len(_TOOL_PHRASES),
        "recent_window": _RECENT_WINDOW,
        "activity_bias": _ACTIVITY_BIAS,
    }


# ─── self-test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== pool sizes ===")
    for k, v in pool_size_report().items():
        print(f"  {k}: {v}")
    print("\n=== simulate 3 turns of 6 filler picks each, with recent-window persisting ===")
    reset()
    for turn in range(1, 4):
        print(f"\nturn {turn}:")
        reset()  # clears last_tool but NOT recent_phrases
        for _ in range(6):
            print(" ", next_phrase())
    print("\n=== after read_sheet — 10 phrases (50% tool, 50% filler) ===")
    reset()
    record_tool("read_sheet")
    for _ in range(10):
        print(" ", next_phrase())
