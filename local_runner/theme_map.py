"""Theme to ticker mapping for the hot theme dashboard.

THEMES are NARRATIVES — where money is rotating, not GICS sectors.
A ticker can (and usually should) appear in multiple themes.
Order below = order in the dashboard sidebar.

UNIVERSE is the full hand-picked ticker list from Dan (~470).
Any ticker in UNIVERSE that doesn't appear in any THEMES list shows
up in the "Ungrouped" section at the bottom of the dashboard.
"""

THEMES = {
    # ═══════════════════════════════════════════════════
    #  AI BUILDOUT — the dominant 2024-2026 narrative
    # ═══════════════════════════════════════════════════
    "ai_compute":               ["NVDA", "AMD", "AVGO", "MRVL", "TXN", "QCOM", "MCHP", "ON", "NXPI", "INTC", "MU", "GFS", "WOLF", "ALAB", "RMBS", "MPWR", "LSCC", "SWKS", "QRVO", "SITM", "ALGM", "CRUS", "SMTC", "AMKR", "SYNA", "TSEM", "CRDO", "NVTS", "MXL"],
    "ai_optics":                ["AAOI", "AXTI", "CIEN", "COHR", "CRDO", "FN", "GLW", "LITE", "LWLG", "MKSI", "MTSI", "POET", "VIAV", "MRVL", "AVGO"],
    "ai_networking":            ["ANET", "CRDO", "ALAB", "AAOI", "FN", "CIEN", "NET", "AKAM", "UI", "IDCC"],
    "ai_connectivity":          ["ALAB", "MRVL", "AVGO", "MXL", "CRDO", "SLAB", "MPWR", "ADI", "TXN"],
    "semiconductor_ip":         ["ARM", "SNPS", "CDNS", "CEVA"],
    "memory_cycle":             ["MU", "SNDK", "STX", "WDC", "MRAM"],
    "semi_equipment":           ["LRCX", "AMAT", "KLAC", "ENTG", "FORM", "TER", "NVMI", "ONTO", "AEHR", "ACLS", "MKSI"],
    "datacenter_buildout":      ["VRT", "DELL", "HPE", "HPQ", "CLS", "SMCI", "JBL", "FLEX", "SANM", "PENG", "ANET", "CRDO", "MOD", "AAON", "BE", "MPWR", "NVT", "AEIS", "WSO", "CDW"],
    "ai_gpu_clouds":            ["CRWV", "NBIS", "APLD", "IREN", "RXT", "DOCN"],
    "neoclouds":                ["CRWV", "NBIS", "APLD", "IREN", "CIFR", "WULF", "CORZ", "BTDR", "HIVE", "HUT", "RIOT", "MARA", "CLSK", "ORCL", "TLN"],
    "datacenters_gpu_hosting":  ["NBIS", "IREN", "HUT", "RIOT", "CRWV", "WULF", "CIFR", "APLD", "CORZ", "BTDR", "HIVE", "CLSK", "MARA", "KEEL"],
    "semiconductor_testing":    ["TER", "KLAC", "FORM", "ONTO", "NVMI", "AEHR", "COHU", "CAMT", "ICHR", "UCTT"],
    "ai_apps_platforms":        ["PLTR", "APP", "TEM", "BBAI", "SOUN", "PATH", "IOT", "DUOL", "INOD", "U", "TTD", "ZETA"],
    "quantum_computing":        ["IONQ", "QBTS", "RGTI", "QUBT"],

    # ═══════════════════════════════════════════════════
    #  POWER & ENERGY — AI's electricity bill
    # ═══════════════════════════════════════════════════
    "power_demand_for_ai":      ["CEG", "VST", "NRG", "TLN", "GEV", "ETN", "VRT", "GNRC", "AEIS", "NVT", "POWL", "MPWR", "MOD", "AAON", "BE", "SOLS"],
    "nuclear_renaissance":      ["CCJ", "OKLO", "SMR", "LEU", "BWXT", "UEC", "UUUU", "CEG", "VST", "NRG", "TLN"],
    "uranium_pure_plays":       ["CCJ", "LEU", "UEC", "UUUU", "DNN"],
    "solar":                    ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "FLNC"],
    "energy_storage":           ["FLNC", "EOSE", "AMPX", "QS"],
    "fuel_cells_hydrogen":      ["FCEL", "PLUG", "BE"],

    # ═══════════════════════════════════════════════════
    #  EV & AUTONOMY
    # ═══════════════════════════════════════════════════
    "ev_makers":                ["TSLA", "RIVN", "LCID", "F", "EZGO"],
    "ev_supply_chain":          ["NVTS", "MOD", "BWA", "APTV", "ALB", "AMPX", "ST", "ALSN"],
    "autonomous_driving":       ["TSLA", "AUR", "IOT", "PATH", "LYFT"],

    # ═══════════════════════════════════════════════════
    #  CRYPTO REVIVAL
    # ═══════════════════════════════════════════════════
    "crypto_platforms":         ["COIN", "HOOD", "CRCL", "GLXY", "DXYZ", "DGXX"],
    "bitcoin_treasury":         ["MSTR", "BMNR"],
    "crypto_miners":            ["MARA", "RIOT", "HUT", "CIFR", "CLSK", "CORZ", "WULF", "IREN", "BMNR"],

    # ═══════════════════════════════════════════════════
    #  FRONTIER AEROSPACE & ROBOTICS
    # ═══════════════════════════════════════════════════
    "drones":                   ["AVAV", "ONDS", "RCAT", "KTOS", "RKLB"],
    "evtol_air_mobility":       ["JOBY", "ACHR"],
    "space_economy":            ["RKLB", "LUNR", "ASTS", "RDW", "DXYZ", "SATS", "VSAT", "PL", "GSAT", "SPIR", "BKSY", "IRDM"],
    "robotics_automation":      ["SYM", "CGNX", "TER", "AVAV", "OUST", "ZBRA"],

    # ═══════════════════════════════════════════════════
    #  DEFENSE & GEOPOLITICAL
    # ═══════════════════════════════════════════════════
    "defense_tech":             ["KTOS", "AVAV", "BWXT", "AXON", "OSK", "KRMN", "RCAT", "ONDS", "RKLB", "TTMI", "LDOS", "BAH", "CACI"],
    "critical_minerals":        ["MP", "USAR", "UAMY", "CRML", "UUUU", "LEU", "ALB", "HBM", "UEC"],
    "specialty_aero_metals":    ["CRS", "ATI", "CSL"],

    # ═══════════════════════════════════════════════════
    #  PRECIOUS METALS & MINING
    # ═══════════════════════════════════════════════════
    "gold_silver_miners":       ["KGC", "EGO", "AG", "BTG", "PAAS", "CDE", "HL", "IAG", "WPM", "AGI", "EQX"],
    "copper":                   ["FCX", "SCCO"],
    "steel_aluminum":           ["CLF", "AA", "ATI"],

    # ═══════════════════════════════════════════════════
    #  CYBERSECURITY & ENTERPRISE SOFTWARE
    # ═══════════════════════════════════════════════════
    "cybersecurity":            ["PANW", "CRWD", "FTNT", "ZS", "OKTA", "NET", "S", "RBRK", "CHKP", "GEN", "AKAM", "Q", "BB", "FSLY"],
    "data_devops_platforms":    ["DDOG", "MDB", "SNOW", "DT", "GTLB", "FROG", "TEAM", "DBX", "ORCL"],
    "cloud_infra":              ["NET", "FSLY", "NTNX", "RXT", "DBX", "DOCN", "AKAM", "GDDY", "TWLO"],
    "productivity_saas":        ["NOW", "WDAY", "MNDY", "TEAM", "INTU", "DOCU", "ZM", "FIG", "HUBS", "KVYO"],
    "vertical_saas":            ["TOST", "VEEV", "COMP", "GWRE", "PCOR", "TYL", "DOCS", "SHOP", "PAYC"],
    "ad_tech_marketing":        ["APP", "TTD", "RDDT", "PINS", "SNAP", "ZETA", "PSKY", "HUBS", "KVYO"],

    # ═══════════════════════════════════════════════════
    #  HEALTHCARE NARRATIVES
    # ═══════════════════════════════════════════════════
    "medtech_devices":          ["PODD", "DXCM", "GKOS", "TMDX", "ALGN", "GMED", "INSM", "BAX"],
    "diagnostics_tools":        ["GH", "NTRA", "ILMN", "RGEN", "TECH", "IQV", "ICLR", "CRL", "BIO", "WAT", "MTD", "DOCS", "RVTY"],
    "managed_care":             ["HUM", "CNC", "OSCR", "MOH", "UHS", "ALHC", "THC", "DVA", "BTSG", "OPCH"],
    "animal_health":            ["ZTS", "ELAN"],

    # ═══════════════════════════════════════════════════
    #  FINTECH & FINANCIALS
    # ═══════════════════════════════════════════════════
    "fintech_disruptors":       ["SOFI", "HOOD", "AFRM", "UPST", "DAVE", "LMND", "CHYM", "FIGR", "RKT", "OPEN", "GRAB", "TE"],
    "payments":                 ["CPAY", "GPN", "RELY"],
    "alt_asset_managers":       ["TPG", "OWL", "ARES", "CG", "LPLA", "SEI", "EVR"],
    "specialty_insurance":      ["RYAN", "KNSL", "LMND"],

    # ═══════════════════════════════════════════════════
    #  RESHORING, GRID, INFRASTRUCTURE
    # ═══════════════════════════════════════════════════
    "reshoring_construction":   ["FIX", "PWR", "MTZ", "IESC", "PRIM", "MYRG", "AGX", "STRL", "ACM", "ECG", "J", "TTEK", "DY", "FLR"],
    "electrical_grid":          ["ETN", "VRT", "GEV", "GNRC", "NRG", "POWL", "AEIS", "NVT", "RRX", "SPXC", "FLS"],
    "hvac_cooling":             ["AAON", "MOD", "WSO"],
    "building_products":        ["FBIN", "POOL", "FND", "WHR", "IBP", "SWK", "MHK", "RH", "OC", "SITE", "BLDR", "TPL", "TSCO", "CSL", "SN"],
    "industrial_distribution":  ["WCC", "ARW", "CDW", "QXO"],
    "trucking_logistics":       ["KNX", "ODFL", "SAIA", "XPO", "CHRW"],
    "aviation_aero_leasing":    ["FTAI", "FLY"],

    # ═══════════════════════════════════════════════════
    #  ENERGY (TRADITIONAL)
    # ═══════════════════════════════════════════════════
    "oil_gas_ep":               ["APA", "SM", "PBF", "TPL"],
    "oil_gas_services":         ["DINO", "WFRD", "RIG", "LBRT", "KGS", "MUSA"],
    "lng_natgas":               ["VG"],
    "fertilizer":               ["CF", "MOS"],

    # ═══════════════════════════════════════════════════
    #  CONSUMER & TRAVEL
    # ═══════════════════════════════════════════════════
    "energy_drinks_wellness":   ["CELH", "BROS", "COCO", "HIMS", "COKE"],
    "restaurants_fast_casual":  ["CAVA", "WING", "SHAK", "EAT", "BROS"],
    "footwear_apparel":         ["CROX", "ONON", "BOOT", "AS", "GAP", "VFC", "BBWI"],
    "beauty":                   ["ELF", "EL"],
    "discount_retail":          ["DG", "DLTR", "FIVE", "OLLI", "SFM"],
    "ecommerce":                ["ETSY", "RH", "W", "EBAY", "CHWY", "CART", "CVNA", "SHOP", "CPNG", "WIX", "DASH"],
    "online_travel":            ["BKNG", "EXPE"],
    "cruise":                   ["CCL", "NCLH", "RCL", "VIK"],
    "airlines":                 ["AAL", "UAL", "JBLU", "LUV", "ALK"],
    "auto_marketplaces":        ["CVNA", "KMX", "CAR"],
    "sports_betting":           ["DKNG"],
    "streaming_media":          ["SPOT", "ROKU", "PSKY", "SPHR"],
    "gaming_meme":              ["RBLX", "GME"],
    "social_media":             ["SNAP", "PINS", "RDDT"],

    # ═══════════════════════════════════════════════════
    #  REAL ESTATE & PROPTECH
    # ═══════════════════════════════════════════════════
    "real_estate":              ["ARE", "CBRE", "JLL"],
    "proptech":                 ["Z", "COMP", "OPEN", "CSGP"],

    # ═══════════════════════════════════════════════════
    #  ELECTRONIC COMPONENTS, AUTO PARTS, CHEMICALS
    # ═══════════════════════════════════════════════════
    "auto_parts_tech":          ["APTV", "BWA", "LKQ", "MOD", "ALSN", "ST"],
    "electronic_components":    ["VSH", "LFUS", "VICR", "OLED", "IDCC", "APH"],
    "data_analytics_terminals": ["FDS", "TRI", "TYL", "IT", "G", "VRSK", "TRU", "FICO"],
    "consulting_govt":          ["ACN", "EPAM", "CTSH", "BAH", "CACI", "LDOS"],
    "chemicals":                ["DOW", "CE", "WLK", "ESI"],
    "packaging":                ["IP", "SW"],
    "telco_cable":              ["CHTR", "LUMN"],

    # ═══════════════════════════════════════════════════
    #  BIOTECH — placed last, isolated from the rest
    # ═══════════════════════════════════════════════════
    "biotech":                  ["TEM", "ORKA", "RVMD", "AXSM", "CELC", "MIRM", "GH", "NTRA", "PTCT", "ARWR", "BBIO", "ALNY", "INSM", "MDGL", "PRAX", "TVTX", "MRNA", "NTLA", "CYTK", "HALO", "ABVX", "AKAN", "ERAS", "SARO", "ROIV", "HIMS"],
}


# Display labels for each theme key
THEME_LABELS = {
    "ai_compute":               "AI Compute · semis",
    "ai_optics":                "AI Optics · 800G/1.6T",
    "ai_networking":            "AI Networking · DC fabric",
    "ai_connectivity":          "AI Connectivity · analog & mixed signal",
    "semiconductor_ip":         "Semiconductor IP · ARM, EDA",
    "semiconductor_testing":    "Semiconductor Testing · ATE & metrology",
    "neoclouds":                "Neoclouds · GPU-as-a-Service",
    "datacenters_gpu_hosting":  "Datacenters · GPU hosting (miner pivot + neoclouds)",
    "memory_cycle":             "Memory Cycle · HBM/NAND",
    "semi_equipment":           "Semi Equipment · wafer fab cap-ex",
    "datacenter_buildout":      "Datacenter Buildout · servers, cooling, racks",
    "ai_gpu_clouds":            "AI GPU Clouds · alt-cloud capacity",
    "ai_apps_platforms":        "AI Apps & Platforms",
    "quantum_computing":        "Quantum Computing",
    "power_demand_for_ai":      "Power Demand for AI · utilities, grid, generators",
    "nuclear_renaissance":      "Nuclear Renaissance · SMR + utilities",
    "uranium_pure_plays":       "Uranium · pure plays",
    "solar":                    "Solar",
    "energy_storage":           "Energy Storage",
    "fuel_cells_hydrogen":      "Fuel Cells & Hydrogen",
    "ev_makers":                "EV Makers",
    "ev_supply_chain":          "EV Supply Chain",
    "autonomous_driving":       "Autonomous Driving",
    "crypto_platforms":         "Crypto Platforms & Stablecoins",
    "bitcoin_treasury":         "Bitcoin Treasury Cos",
    "crypto_miners":            "Crypto Miners",
    "drones":                   "Drones",
    "evtol_air_mobility":       "eVTOL · Air Mobility",
    "space_economy":            "Space Economy",
    "robotics_automation":      "Robotics & Automation",
    "defense_tech":             "Defense Tech",
    "critical_minerals":        "Critical Minerals & Rare Earths",
    "specialty_aero_metals":    "Specialty Aero Metals",
    "gold_silver_miners":       "Gold & Silver Miners",
    "copper":                   "Copper",
    "steel_aluminum":           "Steel & Aluminum",
    "cybersecurity":            "Cybersecurity",
    "data_devops_platforms":    "Data & DevOps Platforms",
    "cloud_infra":              "Cloud Infra · edge, CDN, hosting",
    "productivity_saas":        "Productivity SaaS",
    "vertical_saas":            "Vertical SaaS",
    "ad_tech_marketing":        "Ad Tech & Marketing",
    "medtech_devices":          "Medtech Devices",
    "diagnostics_tools":        "Diagnostics & Life Science Tools",
    "managed_care":             "Managed Care · payors & providers",
    "animal_health":            "Animal Health",
    "fintech_disruptors":       "Fintech Disruptors",
    "payments":                 "Payments · processors & remittance",
    "alt_asset_managers":       "Alt Asset Managers · PE/credit",
    "specialty_insurance":      "Specialty Insurance",
    "reshoring_construction":   "Reshoring & Construction",
    "electrical_grid":          "Electrical Grid & Power Equipment",
    "hvac_cooling":             "HVAC & Cooling",
    "building_products":        "Building Products & Housing Stack",
    "industrial_distribution":  "Industrial Distribution",
    "trucking_logistics":       "Trucking & Logistics",
    "aviation_aero_leasing":    "Aviation & Aero Leasing",
    "oil_gas_ep":               "Oil & Gas E&P",
    "oil_gas_services":         "Oil & Gas Services",
    "lng_natgas":               "LNG & Natural Gas",
    "fertilizer":               "Fertilizer",
    "energy_drinks_wellness":   "Energy Drinks & Wellness",
    "restaurants_fast_casual":  "Restaurants · fast casual",
    "footwear_apparel":         "Footwear & Apparel",
    "beauty":                   "Beauty",
    "discount_retail":          "Discount Retail",
    "ecommerce":                "E-commerce & DTC",
    "online_travel":            "Online Travel",
    "cruise":                   "Cruise Lines",
    "airlines":                 "Airlines",
    "auto_marketplaces":        "Auto Marketplaces & Rentals",
    "sports_betting":           "Sports Betting",
    "streaming_media":          "Streaming & Media",
    "gaming_meme":              "Gaming & Meme Stocks",
    "social_media":             "Social Media",
    "real_estate":              "Commercial Real Estate",
    "proptech":                 "Proptech",
    "auto_parts_tech":          "Auto Parts & Tech",
    "electronic_components":    "Electronic Components",
    "data_analytics_terminals": "Data Analytics & Terminals",
    "consulting_govt":          "Consulting & Gov Services",
    "chemicals":                "Chemicals",
    "packaging":                "Packaging",
    "telco_cable":              "Telco & Cable",
    "biotech":                  "Biotech · therapeutics, gene editing, GLP-1",
}


# The full hand-picked ticker universe (470 names, $100M+ vol, 3.5+ ADR).
# Used to compute the "Ungrouped" section: any UNIVERSE ticker that doesn't
# appear in any THEMES list is rendered at the bottom of the dashboard.
UNIVERSE = [
    "FTNT", "VSH", "CRWD", "DDOG", "FLEX", "AMD", "HUM", "MXL", "PANW", "CNC",
    "INTC", "PENG", "FROG", "NXPI", "TXN", "ALAB", "HPE", "INOD", "OSCR", "ARW",
    "DOCN", "DGXX", "SYNA", "COCO", "ON", "RKLB", "MU", "STRL", "MTSI", "SMTC",
    "TWLO", "DVA", "RIOT", "MRVL", "GEN", "FCEL", "BTSG", "SITM", "MYRG", "SNDK",
    "STX", "VSAT", "WOLF", "SANM", "BB", "CLSK", "AAON", "EBAY", "TE", "S",
    "AXSM", "GFS", "ST", "DELL", "RBRK", "HUT", "NVTS", "RVMD", "LSCC", "MRAM",
    "GKOS", "NVT", "TTMI", "AKAM", "KEEL", "KGS", "MDB", "BE", "QRVO", "ZS",
    "MCHP", "SWKS", "HRB", "ENPH", "ACLS", "AUR", "KNX", "QCOM", "SM", "TVTX",
    "TSEM", "GH", "NVDA", "SAIA", "RDW", "WDC", "DXYZ", "ROIV", "MARA", "CORZ",
    "WCC", "LTH", "RXT", "ROKU", "CYTK", "AFRM", "OKTA", "PWR", "LUNR", "FPS",
    "AAL", "CGNX", "IONQ", "GTLB", "NBIS", "DINO", "JBL", "FSLR", "WFRD", "POET",
    "BWA", "MKSI", "LRCX", "LFUS", "MPWR", "Q", "IESC", "SMCI", "DXCM", "CPAY",
    "RELY", "ZM", "DBX", "SATS", "FICO", "CRDO", "SFM", "CIEN", "MOH", "FIX",
    "FLY", "ZBRA", "ORCL", "TEAM", "P", "ARWR", "AMAT", "RMBS", "NTNX", "RIG",
    "AXTI", "CROX", "POWL", "SEDG", "PL", "LGN", "APLD", "GNRC", "MUSA", "OUST",
    "VICR", "ALGM", "CELC", "GLXY", "F", "ARES", "WAT", "PAYC", "COHR", "U",
    "LUMN", "KLAC", "AMKR", "ILMN", "ECG", "FLNC", "VIAV", "CLF", "HPQ", "GDDY",
    "ESI", "PLUG", "GLW", "WULF", "MTZ", "TSLA", "IREN", "MSTR", "VIK", "AEHR",
    "CIFR", "HBM", "MNDY", "CRUS", "BAX", "SOUN", "SNOW", "DKNG", "LBRT", "DT",
    "SUNB", "CHRW", "AGX", "EVR", "SEI", "DOCU", "DY", "SPHR", "ZETA", "UAL",
    "JOBY", "EOSE", "QUBT", "ETSY", "ONON", "GEV", "MOD", "HALO", "DAVE", "SOLS",
    "AAOI", "APP", "RVTY", "ODFL", "BIO", "ASTS", "SNAP", "DUOL", "VRT", "BBAI",
    "ARE", "FN", "USAR", "ONTO", "FIG", "BROS", "RUN", "OWL", "XPO", "QS",
    "PTCT", "ICLR", "COMP", "RH", "SWK", "NOW", "CRML", "MDGL", "CART", "QBTS",
    "GPN", "NXT", "AS", "PRAX", "ORKA", "ALK", "CRCL", "FIGR", "APA", "FDS",
    "SARO", "LITE", "IT", "VG", "FORM", "LUV", "CRS", "COIN", "LWLG", "ABVX",
    "AKAN", "CLS", "BAP", "IQV", "RGTI", "CCL", "NTRA", "TER", "NVMI", "CRWV",
    "TLN", "WDAY", "OKLO", "MIRM", "ACHR", "NET", "UPST", "ATI", "IOT", "BTG",
    "EL", "HOOD", "JBLU", "FCX", "PAAS", "PATH", "RDDT", "PSKY", "OC", "FTAI",
    "RRX", "BAH", "PINS", "MHK", "GWRE", "RYAN", "SCCO", "AA", "SPXC", "XNDU",
    "TPG", "DASH", "ONDS", "MP", "ETN", "OLED", "INFQ", "CF", "HIMS", "THC",
    "MRNA", "EZGO", "WSO", "LYFT", "BF.B", "ENTG", "AXON", "BBIO", "FIVE", "SN",
    "AG", "TRU", "GME", "CVNA", "PBF", "ANET", "VEEV", "SMR", "GDC", "NTLA",
    "CAVA", "ACN", "LMND", "CEG", "UAMY", "GMED", "RGEN", "UEC", "TRI", "CDE",
    "TYL", "PLTR", "CG", "BWXT", "TEM", "CRL", "CSL", "EAT", "INTU", "EGO",
    "VRSK", "HL", "WPM", "SYM", "ALB", "ALNY", "TTD", "LPLA", "RCL", "RKT",
    "FND", "BMNR", "UUUU", "HUBS", "OPEN", "CHKP", "ELAN", "COKE", "SOFI", "VST",
    "CBRE", "DOW", "W", "KNSL", "ALGN", "CCJ", "UHS", "LEU", "KGC", "AMPX",
    "TECH", "FLR", "CAR", "KMX", "JLL", "GRAB", "SW", "G", "OLLI", "BOOT",
    "AEIS", "DLTR", "CACI", "BKNG", "EXPE", "FLS", "ALHC", "CSGP", "RBLX", "SHOP",
    "BBWI", "IAG", "SPOT", "GAP", "J", "TTEK", "APH", "AGI", "CHYM", "ERAS",
    "AVAV", "EQX", "FBIN", "KRMN", "IP", "PCOR", "SGI", "QXO", "CDW", "EPAM",
    "CTSH", "TPL", "VFC", "RIVN", "KVYO", "BLDR", "POOL", "DOCS", "LKQ", "TOST",
    "NCLH", "CE", "NRG", "SITE", "ALSN", "DG", "CELH", "WING", "Z", "MTD",
    "IDCC", "APTV", "OSK", "MOS", "MDLN", "ACM", "KTOS", "PODD", "BFAM", "OPCH",
    "INSM", "FSLY", "ELF", "IBP", "CHTR", "WHR", "CHWY", "PRIM", "RCAT", "PLNT",
    "LCID", "WIX", "CPNG", "TMDX", "UI", "WLK", "LDOS", "TSCO", "SHAK", "VISN",
    "ZTS", "COKE",
    # Added 2026-05-20 from theme screenshots Dan supplied
    "SLAB", "ADI", "ARM", "SNPS", "CDNS", "CEVA",
    "GSAT", "SPIR", "BKSY", "IRDM",
    "DNN",
    "BTDR", "HIVE",
    "COHU", "CAMT", "ICHR", "UCTT",
]
