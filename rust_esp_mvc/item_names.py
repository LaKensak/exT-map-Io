"""Short name → display name mapping for Rust held items.

Only includes items that matter for ESP (weapons, tools, medical, throwables).
Unknown short names fall back to the raw short name in the view layer.
"""

ITEM_DISPLAY_NAMES = {
    # ── Rifles ──
    "rifle.ak":                     "AK-47",
    "rifle.ak.diver":               "Abyss AK",
    "rifle.ak.ice":                 "Ice AK",
    "rifle.ak.jungle":              "Jungle AK",
    "rifle.ak.med":                 "Medieval AK",
    "rifle.lr300":                  "LR-300",
    "rifle.lr300.space":            "Space LR",
    "rifle.m39":                    "M39",
    "rifle.semiauto":               "SAR",
    "rifle.sks":                    "SKS",
    "rifle.bolt":                   "Bolt",
    "rifle.l96":                    "L96",

    # ── LMGs ──
    "lmg.m249":                     "M249",
    "hmlmg":                        "HMLMG",
    "minigun":                      "Minigun",

    # ── SMGs ──
    "smg.thompson":                 "Tommy",
    "smg.mp5":                      "MP5",
    "smg.2":                        "Custom SMG",
    "t1_smg":                       "Handmade SMG",

    # ── Pistols ──
    "pistol.m92":                   "M92",
    "pistol.python":                "Python",
    "pistol.semiauto":              "Semi Pistol",
    "pistol.semiauto.a.m15":        "M15",
    "pistol.revolver":              "Revolver",
    "pistol.eoka":                  "Eoka",
    "pistol.nailgun":               "Nailgun",
    "pistol.prototype17":           "Proto 17",
    "pistol.water":                 "Water Pistol",
    "revolver.hc":                  "HC Revolver",
    "blunderbuss":                  "Blunderbuss",

    # ── Shotguns ──
    "shotgun.pump":                 "Pump",
    "shotgun.spas12":               "Spas-12",
    "shotgun.double":               "DB",
    "shotgun.waterpipe":            "Waterpipe",
    "shotgun.m4":                   "M4 Shotgun",
    "krieg.shotgun":                "Krieg Shotgun",

    # ── Bows ──
    "bow.hunting":                  "Bow",
    "bow.compound":                 "Compound",
    "crossbow":                     "Crossbow",
    "minicrossbow":                 "Mini Crossbow",
    "blowpipe":                     "Blowpipe",
    "speargun":                     "Speargun",

    # ── Launchers ──
    "rocket.launcher":              "Rocket",
    "rocket.launcher.dragon":       "Dragon RL",
    "rocket.launcher.rpg7":         "RPG",
    "multiplegrenadelauncher":      "MGL",
    "homingmissile.launcher":       "Homing",
    "flamethrower":                 "Flamethrower",
    "military flamethrower":        "Mil Flamethrower",

    # ── Melee ──
    "knife.combat":                 "Combat Knife",
    "knife.butcher":                "Butcher Knife",
    "knife.skinning":               "Skinning Knife",
    "knife.bone":                   "Bone Knife",
    "knife.bone.obsidian":          "Obsidian Knife",
    "sunken.knife":                 "Sunken Knife",
    "longsword":                    "Longsword",
    "salvaged.sword":               "Sword",
    "salvaged.cleaver":             "Cleaver",
    "machete":                      "Machete",
    "bone.club":                    "Bone Club",
    "mace":                         "Mace",
    "mace.baseballbat":             "Baseball Bat",
    "paddle":                       "Paddle",
    "pitchfork":                    "Pitchfork",
    "spear.wooden":                 "Wood Spear",
    "spear.stone":                  "Stone Spear",
    "boomerang":                    "Boomerang",
    "sickle":                       "Sickle",
    "shovel":                       "Shovel",
    "krieg.chainsword":             "Chainsword",

    # ── Tools ──
    "hatchet":                      "Hatchet",
    "pickaxe":                      "Pickaxe",
    "stone.pickaxe":                "Stone Pick",
    "stonehatchet":                 "Stone Hatchet",
    "rock":                         "Rock",
    "torch":                        "Torch",
    "jackhammer":                   "Jackhammer",
    "chainsaw":                     "Chainsaw",
    "hammer":                       "Hammer",
    "icepick.salvaged":             "Icepick",
    "axe.salvaged":                 "Salvaged Axe",
    "hammer.salvaged":              "Salvaged Hammer",
    "concretehatchet":              "Concrete Hatchet",
    "concretepickaxe":              "Concrete Pick",

    # ── Throwables / Explosives ──
    "grenade.f1":                   "F1 Grenade",
    "grenade.beancan":              "Beancan",
    "grenade.flashbang":            "Flashbang",
    "grenade.molotov":              "Molotov",
    "grenade.smoke":                "Smoke",
    "explosive.satchel":            "Satchel",
    "explosive.timed":              "C4",
    "supply.signal":                "Supply Signal",

    # ── Medical ──
    "syringe.medical":              "Med Syringe",
    "bandage":                      "Bandage",
    "largemedkit":                  "Large Medkit",

    # ── Misc commonly held ──
    "building.planner":             "Building Plan",
    "tool.instant_camera":          "Camera",
    "fun.guitar":                   "Guitar",
    "toolgun":                      "Toolgun",
}
