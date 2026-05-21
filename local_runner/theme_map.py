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
    #  MEGA-CAP TECH — the index-anchor names
    # ═══════════════════════════════════════════════════
    "mag7":                     ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA"],

    # ═══════════════════════════════════════════════════
    #  AI BUILDOUT — the dominant 2024-2026 narrative
    # ═══════════════════════════════════════════════════
    "ai_compute":               [
        "NVDA", "AMD", "AVGO", "MRVL", "TXN", "QCOM", "INTC", "MU", "NXPI",
        "MCHP",   # rationale: embedded MCUs + power management for AI server BMC and peripheral cards
        "ON",     # rationale: power-management semiconductors for AI server boards
        "GFS",    # rationale: non-leading-edge foundry capacity (analog/RF/power) supporting AI buildout
        "WOLF",   # rationale: silicon-carbide and GaN power devices for high-density AI racks
        "ALAB",   # rationale: cross-listed in ai_networking + ai_connectivity; included for end-market AI exposure
        "RMBS",   # rationale: memory interface IP (DDR5 / HBM controllers) tied directly to AI memory demand
        "MPWR",   # rationale: high-density analog power-management ICs designed into GPU servers
        "LSCC",   # rationale: low-power FPGAs for AI server board management and control
        "SWKS", "QRVO",
        "SITM",   # rationale: precision MEMS timing ICs for high-speed AI interconnects
        "ALGM", "CRUS", "SMTC",
        "AMKR",   # rationale: outsourced advanced packaging (chiplets, HBM stacks) for AI accelerators
        "SYNA",
        "TSEM",   # rationale: specialty analog foundry feeding the AI semiconductor ecosystem
        "CRDO",   # rationale: cross-listed in ai_networking + ai_optics + ai_connectivity; high-speed serdes/AECs
        "NVTS",   # rationale: GaN power-conversion ICs for high-efficiency AI racks
        "MXL",
        "TSMX",   # rationale: 2x leveraged TSM ETN (directional foundry exposure)
        "SOXL",   # rationale: 3x leveraged semiconductor ETF (directional theme exposure)
        "USD",    # rationale: 2x leveraged semiconductor ETF
        "XSD",    # rationale: equal-weight semiconductor ETF
        "PSI",    # rationale: Invesco semiconductor ETF
    ],
    "ai_optics":                [
        "AAOI", "COHR", "LITE", "POET", "MTSI",
        "AXTI",   # rationale: indium phosphide substrates feeding optical transceiver supply chain
        "CIEN",   # rationale: optical networking gear including WAN coherent + DC interconnect
        "CRDO",   # rationale: high-speed optical DSPs for 800G/1.6T transceivers (cross-listed)
        "FN",     # rationale: contract manufacturer of optical modules for Cisco/Ciena/Lumentum
        "GLW",    # rationale: Corning optical fiber + photonics glass feedstock
        "LWLG",   # rationale: electro-optic polymer modulators (next-gen photonic IC)
        "MKSI",   # rationale: photonics + vacuum tooling for optical-component manufacturing
        "VIAV",   # rationale: optical network test/measurement instruments
        "MRVL",   # rationale: optical DSPs for transceivers (cross-listed)
        "AVGO",   # rationale: optical PHYs/DSPs and silicon photonics roadmap (cross-listed)
    ],
    "ai_networking":            [
        "ANET", "CRDO", "AAOI",
        "ALAB",   # rationale: Scorpio smart fabric switch entered $20B AI switching market in early 2026
        "FN",     # rationale: contract mfr of networking + optical gear for top OEMs
        "CIEN",   # rationale: optical networking that stitches DC fabric across buildings (WAN/metro)
        "NET",    # rationale: Cloudflare global edge network adjacent to AI inference traffic
        "AKAM",   # rationale: CDN + edge compute infrastructure
        "UI",     # rationale: Ubiquiti enterprise networking (less DC-fabric, more SMB/edge)
    ],
    "ai_connectivity":          [
        "ALAB",   # rationale: PCIe 6 / CXL / NVLink Fusion retimers and Aries smart cable modules
        "MRVL",   # rationale: Alaska P PCIe Gen 6 retimers + custom interconnect ASICs
        "AVGO",   # rationale: PCIe and high-speed serdes for hyperscaler custom silicon
        "MXL",    # rationale: high-speed communications SoCs (PAM4, broadband)
        "CRDO",   # rationale: AECs / PCIe retimers for short-reach high-speed links
        "SLAB",   # rationale: mixed-signal MCUs (more IoT/edge than DC; included for connectivity overlap)
        "MPWR",   # rationale: low-noise power for high-speed serdes/PCIe rails
        "ADI",    # rationale: high-speed converters and analog front-ends for AI signal paths
        "TXN",    # rationale: power and analog support for AI accelerator cards
    ],
    "semiconductor_ip":         [
        "ARM", "SNPS", "CDNS",
        "CEVA",   # rationale: DSP / wireless / AI inference IP licensed to chipmakers (smaller royalty model)
    ],
    "memory_cycle":             [
        "MU", "SNDK",
        "STX",    # rationale: high-capacity HDDs for AI inference cold storage (mass-capacity supercycle)
        "WDC",    # rationale: post-Sandisk spinoff WD now pure-play hard drives + enterprise storage
        "MRAM",   # rationale: Everspin MRAM — niche persistent memory adjacent to AI workloads
        "P",      # rationale: Everpure data-storage software (purity SaaS layer over memory hardware)
        "DRAM",   # rationale: Roundhill Memory ETF (basket exposure to memory cycle)
    ],
    "semi_equipment":           [
        "LRCX", "AMAT", "KLAC", "ONTO",
        "ENTG",   # rationale: ultra-pure materials and consumables for wafer fabs (high-volume razor)
        "FORM",   # rationale: probe cards for wafer test (capex-correlated)
        "TER",    # rationale: ATE for chip test (cross-listed with semiconductor_testing)
        "NVMI",   # rationale: process-control / metrology for fab yield
        "AEHR",   # rationale: SiC + burn-in test systems (cross-listed with semiconductor_testing)
        "ACLS",   # rationale: ion-implant systems for mature-node + power semi fabs
        "MKSI",   # rationale: vacuum + power-delivery subsystems for fab tools
        "ICHR",   # rationale: precision fluid-delivery subsystems for fab equipment
        "UCTT",   # rationale: gas-delivery and chamber subsystems for fab equipment
        "Q",      # rationale: Qnity Electronics — materials/solutions supplied directly into semi-fab and electronics manufacturing
    ],
    "datacenter_buildout":      [
        "VRT", "DELL", "HPE", "SMCI", "ANET",
        "HPQ",    # rationale: workstations + commercial PC supply chain into hyperscaler office/edge fleets
        "CLS",    # rationale: Celestica builds custom hyperscaler racks (e.g. Google TPU board assembly)
        "JBL",    # rationale: Jabil Intelligent Infrastructure segment — server/network EMS for hyperscalers
        "FLEX",   # rationale: Flex data-center supply chain + manufacturing for cloud customers
        "SANM",   # rationale: Sanmina integrated manufacturing for cloud/data-center hardware
        "PENG",   # rationale: Penguin Solutions Advanced Computing + Integrated Memory for enterprise/cloud
        "CRDO",   # rationale: AECs inside the rack (cross-listed in ai_networking/optics/connectivity)
        "MOD",    # rationale: Modine — liquid-cooling thermal solutions for AI racks (mission-critical)
        "AAON",   # rationale: precision cooling units for high-density data centers
        "BE",     # rationale: Bloom Energy solid-oxide fuel cells for on-site DC power
        "MPWR",   # rationale: rack power management (cross-listed in ai_compute/connectivity)
        "NVT",    # rationale: nVent electrical connection + thermal management products for DC
        "AEIS",   # rationale: Advanced Energy precision power-conversion for data centers
        "CDW",    # rationale: IT-solution channel into hyperscalers + enterprise data centers
        "LUMN",   # rationale: fiber + private connectivity stitching DCs (PrivateConnect for AI)
        "FPS",    # rationale: Forgent Power Solutions — electrical distribution gear for DCs and grid
        "VISN",   # rationale: Vistance Networks infrastructure for comms + data center sites
    ],
    "ai_compute_clouds":        [
        "CRWV", "NBIS", "APLD",
        "IREN",   # rationale: Bitcoin miner pivot → $9.7B Microsoft deal + Nvidia partnership for 5GW AI
        "RXT",    # rationale: Rackspace hybrid-cloud + AI operations services
        "DOCN",   # rationale: DigitalOcean agentic-inference cloud platform (SMB-oriented)
        "CIFR",   # rationale: Cipher Mining — Bitcoin miner with HPC hosting expansion
        "WULF",   # rationale: TeraWulf — Bitcoin miner pivoting to AI data-center hosting
        "CORZ",   # rationale: Core Scientific — Bitcoin mining infra + colocation hosting (CoreWeave customer)
        "BTDR",   # rationale: Bitdeer — blockchain + HPC infrastructure
        "HIVE",   # rationale: HIVE Digital — green-powered data centers (Bitcoin + HPC)
        "HUT",    # rationale: Hut 8 — power + digital infrastructure with HPC pivot
        "RIOT",   # rationale: Riot Platforms — Bitcoin miner with HPC site optionality
        "MARA",   # rationale: MARA Holdings — Bitcoin + energy infrastructure with HPC ambition
        "CLSK",   # rationale: CleanSpark — Bitcoin miner with energy infrastructure assets
        "TLN",    # rationale: Talen Energy — independent power producer with Amazon nuclear-PPA for AI DC
        "KEEL",   # rationale: Keel Infrastructure — purpose-built HPC/AI data-center operator
    ],
    "semiconductor_testing":    [
        "TER", "FORM",
        "KLAC",   # rationale: KLA process-control / metrology (cross-listed in semi_equipment)
        "ONTO",   # rationale: Onto Innovation — wafer inspection + metrology (cross-listed)
        "NVMI",   # rationale: Nova — process-control / yield metrology (cross-listed)
        "AEHR",   # rationale: Aehr Test — wafer-level burn-in + silicon-photonics test (AI DC ramp)
        "COHU",   # rationale: Cohu — handlers + contactors for semiconductor test
        "CAMT",   # rationale: Camtek — inspection + metrology for advanced packaging (HBM yield)
    ],
    "ai_apps_platforms":        [
        "PLTR", "APP",
        "TEM",    # rationale: Tempus AI — clinical-data layer for oncology + precision medicine (cross-listed in biotech)
        "BBAI",   # rationale: BigBear.ai — AI decision-intelligence for government/defense applications
        "SOUN",   # rationale: SoundHound — independent voice AI platform
        "PATH",   # rationale: UiPath — RPA + agentic-AI automation platform (NOT autonomous driving)
        "IOT",    # rationale: Samsara — physical-operations data platform (fleet IoT + AI; cross-listed in autonomous_mobility)
        "DUOL",   # rationale: Duolingo — AI-powered language learning (consumer AI app)
        "INOD",   # rationale: Innodata — data-engineering services for AI training datasets
        "U",      # rationale: Unity Software — real-time 3D + AI development platform
        "TTD",    # rationale: Trade Desk — AI-driven programmatic ad-tech (cross-listed in ad_tech_marketing)
        "ZETA",   # rationale: Zeta Global — AI-driven marketing cloud platform
    ],
    "quantum_computing":        [
        "IONQ", "QBTS", "RGTI", "QUBT",
        "INFQ",   # rationale: Infleqtion — neutral-atom quantum computing systems
        "XNDU",   # rationale: Xanadu — photonic quantum computing hardware + software
    ],

    # ═══════════════════════════════════════════════════
    #  POWER & ENERGY — AI's electricity bill
    # ═══════════════════════════════════════════════════
    "power_demand_for_ai":      [
        "CEG", "VST", "NRG", "TLN",
        "GEV",    # rationale: GE Vernova — gas/wind turbines + grid equipment supplying utilities expanding for AI load
        "ETN",    # rationale: Eaton — power-management gear for DC + grid
        "VRT",    # rationale: Vertiv power/cooling at the rack edge (cross-listed in datacenter_buildout)
        "GNRC",   # rationale: Generac backup generators + grid-stability products for sites near AI DCs
        "AEIS",   # rationale: Advanced Energy precision power conversion (cross-listed in datacenter_buildout)
        "NVT",    # rationale: nVent electrical connection/protection (cross-listed in datacenter_buildout)
        "POWL",   # rationale: Powell Industries — custom switchgear for utilities and DCs
        "MPWR",   # rationale: power-management ICs (cross-listed in ai_compute/connectivity/datacenter_buildout)
        "MOD",    # rationale: Modine thermal/cooling (cross-listed in datacenter_buildout)
        "AAON",   # rationale: precision cooling (cross-listed in datacenter_buildout)
        "BE",     # rationale: Bloom Energy on-site fuel cells (cross-listed in datacenter_buildout)
        "SOLS",   # rationale: Solstice Advanced Materials — specialty chemicals (refrigerants/electronic materials) into power/cooling
    ],
    "nuclear_renaissance":      [
        "CCJ", "OKLO", "SMR", "LEU", "BWXT", "UEC",
        "UUUU",   # rationale: Energy Fuels — uranium + REE recovery (cross-listed in critical_minerals/uranium_pure_plays)
        "CEG", "VST", "NRG", "TLN",   # nuclear-utility cross-listings (also in power_demand_for_ai)
        "NLR",    # rationale: VanEck Uranium & Nuclear ETF (basket exposure)
    ],
    "uranium_pure_plays":       [
        "CCJ", "LEU", "UEC", "UUUU",
        "DNN",    # rationale: Denison Mines — Athabasca-basin uranium exploration/development (Canadian)
        "URA",    # rationale: Global X Uranium ETF (basket exposure across miners + nuclear-fuel cycle)
        "URNM",   # rationale: Sprott Uranium Miners ETF (basket; concentrated in miners)
    ],
    "solar":                    [
        "FSLR", "ENPH",
        "SEDG",   # rationale: SolarEdge — residential + C&I solar inverters and optimizers
        "RUN",    # rationale: Sunrun — residential solar installer + storage (post-25D credit expiry headwind)
        "NXT",    # rationale: Nextpower — utility-scale solar tracker systems
        "TE",     # rationale: T1 Energy — US + Norway solar module and cell manufacturing
    ],
    "energy_storage":           [
        "FLNC",
        "EOSE",   # rationale: Eos — zinc-bromide long-duration energy storage (US-made; Cerberus IPP JV)
        "AMPX",   # rationale: Amprius silicon-anode lithium-ion batteries (cross-listed in ev_supply_chain)
        "QS",     # rationale: QuantumScape — solid-state lithium-metal battery technology (pre-commercial)
    ],
    "fuel_cells_hydrogen":      [
        "PLUG",
        "FCEL",   # rationale: FuelCell Energy — stationary fuel cells (utility + industrial)
        "BE",     # rationale: Bloom Energy — solid-oxide fuel cells (cross-listed in power_demand_for_ai / datacenter_buildout)
    ],

    # ═══════════════════════════════════════════════════
    #  EV & AUTONOMY
    # ═══════════════════════════════════════════════════
    "ev_makers":                [
        "TSLA", "RIVN", "LCID",
        "F",      # rationale: Ford — Lightning + Mach-E + Maverick Hybrid (legacy OEM EV exposure)
        "EZGO",   # rationale: EZGO Technologies — Chinese e-bikes + light EVs (smaller-cap)
    ],
    "ev_supply_chain":          [
        "BWA",
        "NVTS",   # rationale: Navitas GaN power conversion for EVs (cross-listed in ai_compute)
        "MOD",    # rationale: Modine thermal-management content for EV batteries (cross-listed)
        "APTV",   # rationale: Aptiv high-voltage architecture + ADAS / autonomous platforms
        "ALB",    # rationale: Albemarle lithium for EV batteries (cross-listed in critical_minerals)
        "AMPX",   # rationale: Amprius silicon-anode lithium-ion batteries for high-energy applications
        "ST",     # rationale: Sensata — sensors + protection for EV powertrain / battery monitoring
        "ALSN",   # rationale: Allison Transmission — commercial-vehicle EV transmissions / propulsion
    ],
    "autonomous_mobility":      [
        "TSLA",   # rationale: FSD + Robotaxi initiative (cross-listed in ev_makers / mag7)
        "AUR",    # rationale: Aurora Innovation — driverless trucks (Dallas↔Houston commercial)
        "IOT",    # rationale: Samsara — fleet IoT + AI for connected operations (cross-listed)
        "LYFT",   # rationale: Lyft — ride-hailing platform positioned for robotaxi distribution
        "JOBY",   # rationale: Joby Aviation — eVTOL air-mobility (urban air taxi)
        "ACHR",   # rationale: Archer Aviation — eVTOL air-mobility platform + military pivot
    ],

    # ═══════════════════════════════════════════════════
    #  CRYPTO REVIVAL
    # ═══════════════════════════════════════════════════
    "crypto_platforms":         [
        "COIN", "HOOD",
        "CRCL",   # rationale: Circle Internet Group — USDC stablecoin issuer; OCC conditional trust-bank charter
        "GLXY",   # rationale: Galaxy Digital — digital-asset trading/asset-management + crypto-DC infrastructure
        "DXYZ",   # rationale: Destiny Tech100 — pre-IPO tech basket including OpenAI/SpaceX/crypto-adjacent privates
        "DGXX",   # rationale: Digi Power X — energy infra for crypto/HPC data centers (platform-adjacent)
    ],
    "bitcoin_treasury":         [
        "MSTR",
        "BMNR",   # rationale: Bitmine Immersion Technologies — Ethereum treasury equity proxy (same playbook as MSTR on ETH)
    ],
    "crypto_miners":            [
        "MARA", "RIOT", "HUT", "CLSK",
        "CIFR",   # rationale: Cipher Mining — Bitcoin mining + HPC hosting pivot (cross-listed in ai_compute_clouds)
        "CORZ",   # rationale: Core Scientific — colocation + Bitcoin (CoreWeave customer; cross-listed in ai_compute_clouds)
        "WULF",   # rationale: TeraWulf — Bitcoin + AI data-center hosting (cross-listed in ai_compute_clouds)
        "IREN",   # rationale: IREN — Bitcoin pivot to AI ($9.7B MSFT deal; cross-listed in ai_compute_clouds)
        "BMNR",   # rationale: Bitmine — Ethereum treasury (cross-listed in bitcoin_treasury)
    ],

    # ═══════════════════════════════════════════════════
    #  FRONTIER AEROSPACE & ROBOTICS
    # ═══════════════════════════════════════════════════
    "drones":                   [
        "AVAV", "RCAT",
        "ONDS",   # rationale: Ondas Holdings — private wireless + counter-drone systems for autonomous ops
        "KTOS",   # rationale: Kratos Valkyrie CCA (Collaborative Combat Aircraft) program of record; cross-listed in defense_tech
        "RKLB",   # rationale: Rocket Lab — space launch + spacecraft (drone-adjacent for ISR; cross-listed in defense_tech/space_economy)
    ],
    "space_economy":            [
        "RKLB", "ASTS",
        "LUNR",   # rationale: Intuitive Machines — lunar landers + space infrastructure / services
        "RDW",    # rationale: Redwire — critical space components / infrastructure for gov + commercial
        "DXYZ",   # rationale: Destiny Tech100 — pre-IPO basket with SpaceX exposure (cross-listed in crypto_platforms)
        "SATS",   # rationale: EchoStar — GEO satellite operator + spectrum holdings
        "VSAT",   # rationale: Viasat — broadband + government satellite communications
        "PL",     # rationale: Planet Labs — Earth-observation satellite constellation (PBC)
        "GSAT",   # rationale: Globalstar — mobile satellite services + Apple iPhone partnership
        "SPIR",   # rationale: Spire Global — subscription space-data / analytics (LEO constellation)
        "BKSY",   # rationale: BlackSky — high-revisit Earth-observation imagery
        "IRDM",   # rationale: Iridium — global LEO voice + data communications
        "FLY",    # rationale: Firefly Aerospace — small-launch + lunar lander programs
        "NASA",   # rationale: ETF — space-economy basket exposure
        "UFO",    # rationale: Procure Space ETF — space-economy basket exposure
    ],
    "robotics_automation":      [
        "SYM",
        "CGNX",   # rationale: Cognex — machine-vision systems for factory + warehouse automation
        "TER",    # rationale: Teradyne — owns Universal Robots collaborative-robot business (cross-listed in testing/equipment)
        "AVAV",   # rationale: AeroVironment — military robotic systems (cross-listed in drones/defense_tech)
        "OUST",   # rationale: Ouster — lidar sensors for industrial + automotive autonomy
        "ZBRA",   # rationale: Zebra Technologies — automatic-identification + warehouse robotics
    ],

    # ═══════════════════════════════════════════════════
    #  DEFENSE & GEOPOLITICAL
    # ═══════════════════════════════════════════════════
    "defense_tech":             [
        "KTOS", "AVAV", "BWXT", "AXON", "RCAT", "ONDS",
        "OSK",    # rationale: Oshkosh — purpose-built military vehicles (tactical trucks, JLTV)
        "KRMN",   # rationale: Karman Holdings — mission-critical aerospace structures for defense/space
        "RKLB",   # rationale: Rocket Lab — launch + spacecraft for defense space architectures (cross-listed in space_economy)
        "TTMI",   # rationale: TTM Technologies — RF / microwave PCBs for defense radar + EW systems
        "LDOS",   # rationale: Leidos — defense IT and mission solutions (legacy services)
        "BAH",    # rationale: Booz Allen Hamilton — AI/digital consulting for DoD + intelligence
        "CACI",   # rationale: CACI International — gov IT and intel-mission services
        "SARO",   # rationale: StandardAero — aerospace engine MRO for military + commercial fleets
    ],
    "critical_minerals":        [
        "MP", "USAR",
        "UAMY",   # rationale: US Antimony — antimony + zeolite + precious metals (defense + tech industrial inputs)
        "CRML",   # rationale: Critical Metals — Greenland REE + Austrian lithium exploration/development
        "UUUU",   # rationale: Energy Fuels — uranium + REE separation at White Mesa (cross-listed in nuclear/uranium)
        "LEU",    # rationale: Centrus — uranium enrichment in the critical-minerals fuel-cycle bucket (cross-listed)
        "ALB",    # rationale: Albemarle — lithium for energy storage / EV batteries (critical-minerals overlap)
        "HBM",    # rationale: Hudbay Minerals — copper / zinc / precious metals (industrial-critical minerals)
        "UEC",    # rationale: Uranium Energy — uranium pure-play (cross-listed in uranium_pure_plays/nuclear)
    ],

    # ═══════════════════════════════════════════════════
    #  METALS & MINING
    # ═══════════════════════════════════════════════════
    "gold_silver_miners":       [
        "KGC", "EGO", "AG", "BTG", "PAAS", "CDE", "HL", "IAG", "AGI", "EQX",
        "WPM",    # rationale: Wheaton — royalty/streaming model (no operational risk, leverage to commodity price)
        "NUGT",   # rationale: Direxion 2x gold miners ETF (leveraged exposure)
        "JNUG",   # rationale: Direxion 2x junior gold miners ETF (high-beta exploration leverage)
        "GDXU",   # rationale: MicroSectors 3x gold miners ETN
        "SIL",    # rationale: Global X Silver Miners ETF (basket exposure)
        "SILJ",   # rationale: Amplify Junior Silver Miners ETF (junior leverage)
    ],
    "industrial_metals":        [
        "FCX",    # rationale: Freeport-McMoRan — copper + gold mining (electrification + AI grid build copper demand)
        "SCCO",   # rationale: Southern Copper — pure-play copper producer with low-cost Peruvian + Mexican mines
        "CLF",    # rationale: Cleveland-Cliffs — US steel + iron ore (Section 232 tariff beneficiary)
        "AA",     # rationale: Alcoa — bauxite/alumina/aluminum (Section 232 50% aluminum tariff)
        "ATI",    # rationale: ATI — specialty alloys + complex components (aerospace + defense materials)
        "CRS",    # rationale: Carpenter Technology — specialty premium alloys for aerospace + defense
    ],

    # ═══════════════════════════════════════════════════
    #  CYBERSECURITY & ENTERPRISE SOFTWARE
    # ═══════════════════════════════════════════════════
    "cybersecurity":            [
        "PANW", "CRWD", "FTNT", "ZS", "OKTA",
        "NET",    # rationale: Cloudflare zero-trust + edge security (cross-listed in ai_networking/cloud_infra)
        "S",      # rationale: SentinelOne endpoint security platform
        "RBRK",   # rationale: Rubrik data-security / cyber-resilience platform
        "CHKP",   # rationale: Check Point Software — perimeter + cloud security
        "GEN",    # rationale: Gen Digital — Norton + LifeLock consumer cyber-safety
        "AKAM",   # rationale: Akamai web security + DDoS protection (cross-listed in ai_networking/cloud_infra)
        "BB",     # rationale: BlackBerry — cybersecurity + IoT platforms (Cylance/QNX)
        "FSLY",   # rationale: Fastly edge cloud / security (cross-listed in cloud_infra)
    ],
    "data_devops_platforms":    [
        "DDOG", "MDB", "SNOW",
        "DT",     # rationale: Dynatrace — full-stack observability for cloud + AI workloads
        "GTLB",   # rationale: GitLab — end-to-end DevSecOps platform
        "FROG",   # rationale: JFrog — software supply-chain artifact + binary management
        "TEAM",   # rationale: Atlassian — Jira/Confluence collaboration + DevOps tooling
        "DBX",    # rationale: Dropbox — content collaboration + AI-powered productivity layer (cross-listed in cloud_infra)
        "ORCL",   # rationale: Oracle — enterprise database + cloud infrastructure (OCI; second-source vs hyperscalers)
    ],
    "cloud_infra":              [
        "NET", "FSLY", "AKAM",
        "NTNX",   # rationale: Nutanix Cloud Platform — hybrid multicloud + GPT-in-a-Box for enterprise AI
        "RXT",    # rationale: Rackspace FAIR multi-cloud AI hosting (cross-listed in ai_compute_clouds)
        "DBX",    # rationale: Dropbox — collaboration cloud (cross-listed in data_devops_platforms)
        "DOCN",   # rationale: DigitalOcean — SMB-focused cloud + agentic inference (cross-listed in ai_compute_clouds)
        "GDDY",   # rationale: GoDaddy — cloud-based domains + SMB website/commerce stack
        "TWLO",   # rationale: Twilio customer-engagement platform (cloud comms APIs)
        "MSFU",   # rationale: 2x leveraged MSFT ETF (directional exposure to Azure cloud)
    ],
    "productivity_saas":        [
        "NOW", "WDAY", "MNDY", "HUBS",
        "TEAM",   # rationale: Atlassian (cross-listed in data_devops_platforms; productivity overlap)
        "INTU",   # rationale: Intuit — QuickBooks + TurboTax + Credit Karma (SMB/consumer financial productivity)
        "DOCU",   # rationale: DocuSign — e-signature + agreement-management workflows
        "ZM",     # rationale: Zoom AI-first work platform (video + AI Companion)
        "FIG",    # rationale: Figma — collaborative design platform (recent IPO)
        "KVYO",   # rationale: Klaviyo — e-commerce marketing automation SaaS (cross-listed in ad_tech_marketing)
    ],
    "vertical_saas":            [
        "TOST", "VEEV", "GWRE", "SHOP",
        "PCOR",   # rationale: Procore — cloud-based construction management software
        "TYL",    # rationale: Tyler Technologies — vertical software for state/local government
        "DOCS",   # rationale: Doximity — digital platform for medical professionals (vertical SaaS for healthcare)
        "PAYC",   # rationale: Paycom — vertical SaaS for HCM / payroll
    ],
    "ad_tech_marketing":        [
        "APP", "TTD",
        "RDDT",   # rationale: Reddit — data + ads (cross-listed in social_media; high-signal LLM training data licensing)
        "PINS",   # rationale: Pinterest visual search + Media Network Connect non-endemic ads (cross-listed in social_media)
        "SNAP",   # rationale: Snap programmatic ad platform (cross-listed in social_media)
        "ZETA",   # rationale: Zeta Global omnichannel marketing cloud (cross-listed in ai_apps_platforms)
        "HUBS",   # rationale: HubSpot CRM + marketing hub (cross-listed in productivity_saas)
        "KVYO",   # rationale: Klaviyo e-commerce email/SMS marketing (cross-listed in productivity_saas)
        "GGLL",   # rationale: 2x leveraged GOOGL ETF (directional exposure to Google ads platform)
    ],

    # ═══════════════════════════════════════════════════
    #  HEALTHCARE NARRATIVES
    # ═══════════════════════════════════════════════════
    "medtech_devices_consumer_health":  [
        "PODD",   # rationale: Insulet — Omnipod patch pumps + Type 2 pivotal trial in 2026
        "DXCM",   # rationale: Dexcom — CGM (G7 15-Day rollout + Stelo OTC; 11-13% revenue growth guide)
        "GKOS",   # rationale: Glaukos — minimally invasive glaucoma + corneal devices (highest R&D intensity)
        "TMDX",   # rationale: TransMedics — Organ Care System (OCS) for transplant logistics
        "ALGN",   # rationale: Align Technology — Invisalign clear aligners (consumer-orthodontic SaaS-like)
        "GMED",   # rationale: Globus Medical — spine + orthopedic implants (NuVasive merger integration)
        "BAX",    # rationale: Baxter — broad hospital + renal-care portfolio
        "ZTS",    # rationale: Zoetis — animal health (companion + livestock pharmaceuticals)
        "ELAN",   # rationale: Elanco Animal Health — companion + production animal pharmaceuticals
        "MDLN",   # rationale: Medline — med-surg products distribution (recent IPO; private-equity exit)
    ],
    "diagnostics_tools":        [
        "GH", "NTRA", "ILMN",
        "RGEN",   # rationale: Repligen — bioprocessing consumables / filtration for biologics manufacturing
        "TECH",   # rationale: Bio-Techne — reagents / proteins / antibody tools for life-science research
        "IQV",    # rationale: IQVIA — clinical research organization + healthcare data analytics
        "ICLR",   # rationale: ICON — global clinical research organization (CRO)
        "CRL",    # rationale: Charles River Labs — preclinical CRO + research models / animal services
        "BIO",    # rationale: Bio-Rad Laboratories — life-science research instruments + clinical diagnostics
        "WAT",    # rationale: Waters — analytical instruments (mass spec, chromatography) for pharma/QC
        "MTD",    # rationale: Mettler-Toledo — precision instruments + balances / lab automation
        "DOCS",   # rationale: Doximity — physician digital platform (cross-listed in vertical_saas)
        "RVTY",   # rationale: Revvity (ex-PerkinElmer) — life-science tools + diagnostics
        "ARE",    # rationale: Alexandria Real Estate — life-science REIT (lab + biotech property)
    ],
    "managed_care":             [
        "HUM", "CNC", "OSCR", "MOH",
        "UHS",    # rationale: Universal Health Services — acute-care + behavioral health hospitals (provider, not payor)
        "ALHC",   # rationale: Alignment Healthcare — Medicare Advantage tech-driven payor
        "THC",    # rationale: Tenet Healthcare — diversified hospital operator (provider side)
        "DVA",    # rationale: DaVita — kidney dialysis services (provider; Berkshire Hathaway holding)
        "BTSG",   # rationale: BrightSpring Health Services — home + community health (LTC + specialty pharmacy)
        "OPCH",   # rationale: Option Care Health — home + alternate-site infusion services
    ],

    # ═══════════════════════════════════════════════════
    #  FINTECH & FINANCIALS
    # ═══════════════════════════════════════════════════
    "fintech_disruptors":       [
        "SOFI", "HOOD", "AFRM", "UPST",
        "DAVE",   # rationale: Dave — neobank consumer financial services (cash advance + checking)
        "LMND",   # rationale: Lemonade — AI-powered consumer insurance
        "CHYM",   # rationale: Chime — digital consumer banking (~10.2M active members)
        "FIGR",   # rationale: Figure — blockchain-based home equity + lending (HELOC marketplace)
        "RKT",    # rationale: Rocket Companies — digital mortgage / real estate / personal finance
        "GRAB",   # rationale: Grab — Southeast Asia super-app (mobility + delivery + fintech)
        "HRB",    # rationale: H&R Block — assisted + DIY tax with embedded financial-services pivot
    ],
    "financial_services_specialty": [
        "RYAN",   # rationale: Ryan Specialty — E&S wholesale broker (placed 93-94% of E&S premiums historically)
        "KNSL",   # rationale: Kinsale Capital — pure-play E&S underwriter for hard-to-place P&C risks
        "CPAY",   # rationale: Corpay — corporate / commercial payments specialty (fuel + lodging + cross-border)
        "GPN",    # rationale: Global Payments — payment-tech + software for merchants
        "RELY",   # rationale: Remitly — digital cross-border consumer remittances
    ],
    "alt_asset_managers":       [
        "TPG",    # rationale: TPG — diversified PE + credit + impact + real estate alternative asset manager
        "OWL",    # rationale: Blue Owl Capital — direct-lending + GP-stakes specialist (53% PC in fee-earning AUM)
        "ARES",   # rationale: Ares Management — private-credit-heavy alts manager (66% PC of fee-earning AUM)
        "CG",     # rationale: Carlyle Group — balanced PE / credit / real assets (least PC exposure among peers)
        "LPLA",   # rationale: LPL Financial — independent advisor platform (asset-gathering, not pure alts)
        "EVR",    # rationale: Evercore — independent investment banking + advisory (M&A / restructuring)
    ],

    # ═══════════════════════════════════════════════════
    #  RESHORING, GRID, INFRASTRUCTURE, TRANSPORTS
    # ═══════════════════════════════════════════════════
    "reshoring_construction":   [
        "FIX",    # rationale: Comfort Systems — mechanical contractor; $12.45B backlog with 56% from advanced-tech / data center
        "PWR",    # rationale: Quanta Services — record $48.5B backlog (transmission + grid mod for AI/electrification)
        "MTZ",    # rationale: MasTec — $19B 18-month backlog (energy / utility / communications infra)
        "IESC",   # rationale: IES Holdings — integrated electrical + tech systems contractor
        "PRIM",   # rationale: Primoris Services — utility + civil infrastructure construction
        "MYRG",   # rationale: MYR Group — electrical construction (T&D + commercial/industrial)
        "AGX",    # rationale: Argan — EPC for natural-gas + renewables power plants
        "STRL",   # rationale: Sterling Infrastructure — e-infra + transportation + building
        "ACM",    # rationale: AECOM — professional infrastructure consulting / EPC
        "ECG",    # rationale: Everus Construction — electrical + mechanical contractor (MDU Resources spinoff)
        "J",      # rationale: Jacobs Solutions — infrastructure + advanced facilities engineering
        "TTEK",   # rationale: Tetra Tech — water + environmental + sustainable-infrastructure consulting
        "DY",     # rationale: Dycom — telecom + fiber specialty contracting (BEAD + AI-DC interconnect)
        "FLR",    # rationale: Fluor — global EPC for energy / chemicals / mining / govt
        "SUNB",   # rationale: Sunbelt Rentals — equipment rental into construction (Ashtead spinoff)
        "LGN",    # rationale: Legence — engineering / install / maintenance for mission-critical facilities
    ],
    "electrical_grid":          [
        "ETN",    # rationale: Eaton power-management; expanding $1.2B capacity into transformer / switchgear shortage
        "VRT",    # rationale: Vertiv power/cooling 2026 sales +27-29% organic (cross-listed in datacenter_buildout)
        "GEV",    # rationale: GE Vernova gas turbines + transmission gear (cross-listed in power_demand_for_ai)
        "GNRC",   # rationale: Generac backup generators + grid-stability (cross-listed in power_demand_for_ai)
        "NRG",    # rationale: NRG Energy — retail + power generation (cross-listed in power_demand_for_ai)
        "POWL",   # rationale: Powell Industries custom switchgear sold out on grid + DC demand
        "AEIS",   # rationale: Advanced Energy precision power conversion (cross-listed in datacenter_buildout)
        "NVT",    # rationale: nVent electrical connection / thermal management (cross-listed)
        "RRX",    # rationale: Regal Rexnord — power transmission + motion control + climate solutions
        "SPXC",   # rationale: SPX Technologies — engineered HVAC / power-quality / detection solutions
        "FLS",    # rationale: Flowserve — pumps / seals / valves for power-gen + process industries
        "SEI",    # rationale: Solaris Energy Infrastructure — modular power generation equipment
        "FPS",    # rationale: Forgent Power Solutions — electrical distribution for DCs + grid (cross-listed)
    ],
    "hvac_cooling":             [
        "AAON",   # rationale: AAON — precision cooling + $174.5M AI-DC liquid-cooling order (cross-listed)
        "MOD",    # rationale: Modine — $180M AI-DC cooling order, new WI plant scaling Airedale (cross-listed)
        "WSO",    # rationale: Watsco — largest North American HVAC/R distributor (residential + commercial)
    ],
    "building_products":        [
        "FBIN",   # rationale: Fortune Brands Innovations — home + security + digital products (Moen, Master Lock)
        "POOL",   # rationale: Pool Corporation — swimming-pool supplies + equipment distribution
        "FND",    # rationale: Floor & Decor — multi-channel flooring specialty retailer
        "WHR",    # rationale: Whirlpool — home appliances (R&R + new-build exposure)
        "IBP",    # rationale: Installed Building Products — insulation + finishing installations for builders
        "SWK",    # rationale: Stanley Black & Decker — power tools / hand tools / outdoor products
        "MHK",    # rationale: Mohawk Industries — flooring (carpet, tile, hardwood)
        "RH",     # rationale: RH — luxury home furnishings retailer + lifestyle brand
        "OC",     # rationale: Owens Corning — residential / commercial building products (insulation + roofing)
        "SITE",   # rationale: SiteOne Landscape Supply — wholesale distributor to landscape professionals
        "BLDR",   # rationale: Builders FirstSource — building materials supplier (lumber + manufactured components)
        "TSCO",   # rationale: Tractor Supply — rural-lifestyle retail (chicken-coop adjacency to building products)
        "CSL",    # rationale: Carlisle Companies — building-envelope products (roofing systems + insulation)
        "SN",     # rationale: SharkNinja — household appliance design + technology (cooking + cleaning + beauty)
        "NAIL",   # rationale: Direxion 3x homebuilders + supplies ETF (leveraged sector exposure)
    ],
    "industrial_distribution":  [
        "WCC",    # rationale: Wesco — B2B distribution + logistics; DC sales +70% YoY now ~25% of total
        "ARW",    # rationale: Arrow Electronics — global electronic component + IT solutions distribution
        "CDW",    # rationale: CDW — IT solutions distribution to commercial / government / education
        "QXO",    # rationale: QXO — roofing + waterproofing + building products distribution (Brad Jacobs roll-up)
    ],
    "transports":               [
        "KNX",    # rationale: Knight-Swift — diversified TL + LTL carrier (truckload-pressured, LTL buffer)
        "ODFL",   # rationale: Old Dominion — best-in-class LTL carrier; Feb '26 revenue/day -3.3% (early cyclical bottom)
        "SAIA",   # rationale: Saia — LTL carrier in expansion mode (new terminals)
        "XPO",    # rationale: XPO — LTL with AI-driven route optimization; first YoY tonnage gain in 18+ months Feb '26
        "CHRW",   # rationale: C.H. Robinson — asset-light freight brokerage + logistics
        "FTAI",   # rationale: FTAI Aviation — engine leasing + MRO (CFM56 cycle play)
        "CAR",    # rationale: Avis Budget — car + truck rental (cyclical transport-services exposure)
    ],

    # ═══════════════════════════════════════════════════
    #  ENERGY (TRADITIONAL) & MATERIALS
    # ═══════════════════════════════════════════════════
    "oil_gas_ep":               [
        "APA",    # rationale: APA Corporation — independent E&P (Permian + Egypt + North Sea); strategic-options review
        "SM",     # rationale: SM Energy — Midland Basin + Eagle Ford pure-play E&P
        "PBF",    # rationale: PBF Energy — refining + supplying petroleum products (downstream margin proxy)
        "TPL",    # rationale: Texas Pacific Land — Permian royalties + surface + water (+75% YTD on AI-DC land thesis)
        "VG",     # rationale: Venture Global LNG — US Gulf Coast LNG export terminals (Calcasieu Pass + Plaquemines)
    ],
    "oil_gas_services":         [
        "DINO",   # rationale: HF Sinclair — independent refiner + retail / lubricants (midstream-adjacent services)
        "WFRD",   # rationale: Weatherford — global oilfield equipment + services
        "RIG",    # rationale: Transocean — offshore deepwater drilling rigs
        "LBRT",   # rationale: Liberty Energy — premier hydraulic-fracturing fleet + power-infra pivot
        "KGS",    # rationale: Kodiak Gas Services — natural gas contract compression infrastructure
    ],
    "industrial_materials":     [
        "DOW",    # rationale: Dow — diversified materials science (commodity chemicals; cutting 4,500 jobs / $2B savings)
        "CE",     # rationale: Celanese — engineered polymers (auto + construction headwinds, $1B divestiture target)
        "WLK",    # rationale: Westlake — PVC + chlor-alkali (housing + construction exposure)
        "ESI",    # rationale: Element Solutions — specialty chemicals for electronics + industrial
        "IP",     # rationale: International Paper — renewable fiber-based packaging (containerboard)
        "SW",     # rationale: Smurfit Westrock — global paper-based packaging (post-merger)
        "CF",     # rationale: CF Industries — nitrogen fertilizers (ammonia / urea / UAN)
        "MOS",    # rationale: Mosaic — concentrated phosphates + potash for fertilizer
    ],

    # ═══════════════════════════════════════════════════
    #  CONSUMER & TRAVEL
    # ═══════════════════════════════════════════════════
    "energy_drinks_wellness":   [
        "CELH",   # rationale: Celsius Holdings — fitness energy-drink brand (PepsiCo distribution agreement)
        "BROS",   # rationale: Dutch Bros — drive-thru coffee + energy drink growth (cross-listed in restaurants_fast_casual)
        "COCO",   # rationale: Vita Coco — coconut water + PWR LIFT protein water (+37% Q1 2026 net sales)
        "COKE",   # rationale: Coca-Cola Consolidated — largest US Coca-Cola bottler (functional + energy SKU exposure)
    ],
    "restaurants_fast_casual":  [
        "CAVA",   # rationale: CAVA Group — Mediterranean fast-casual; comps pacing above 3-5% guide, 415 restaurants
        "WING",   # rationale: Wingstop — wing-focused fast-casual franchise model with strong unit growth
        "SHAK",   # rationale: Shake Shack — upscale fast-casual burgers
        "EAT",    # rationale: Brinker International — Chili's + Maggiano's (casual-dining; positioned for trade-down)
        "BROS",   # rationale: Dutch Bros — drive-thru coffee (cross-listed in energy_drinks_wellness)
    ],
    "footwear_apparel":         [
        "CROX",   # rationale: Crocs — DTC + brand momentum (Crocs +12.9%, HEYDUDE +8.6% DTC growth Q1)
        "ONON",   # rationale: On Holding — premium running/performance brand; reiterated 23%+ CC FY net sales growth
        "BOOT",   # rationale: Boot Barn — western + work specialty retail footwear / apparel
        "AS",     # rationale: Amer Sports — Arc'teryx + Salomon + Wilson outdoor/sports brand portfolio
        "GAP",    # rationale: Gap Inc. — Gap + Old Navy + Banana Republic + Athleta turnaround story
        "VFC",    # rationale: V.F. Corporation — Vans + The North Face + Timberland (restructuring portfolio)
    ],
    "beauty":                   [
        "ELF",    # rationale: e.l.f. Beauty — affordable trend-led cosmetics; 27 consecutive quarters of net sales growth
        "EL",     # rationale: Estée Lauder — prestige skincare + makeup + fragrance; Beauty Reimagined opex reset
        "BBWI",   # rationale: Bath & Body Works — specialty personal care + home fragrance retailer
    ],
    "discount_retail":          [
        "DG",     # rationale: Dollar General — small-format rural-focused discount (low-income core consumer pressured)
        "DLTR",   # rationale: Dollar Tree — Dollar Tree 3.0 multi-price now ~5,300 stores capturing trade-down
        "FIVE",   # rationale: Five Below — youth + family value retailer in transition
        "OLLI",   # rationale: Ollie's Bargain Outlet — closeout retailer (treasure-hunt format)
        "SFM",    # rationale: Sprouts Farmers Market — specialty grocer (fresh + healthy)
        "MUSA",   # rationale: Murphy USA — value gas-station retailing (Walmart-adjacent fuel + convenience)
    ],
    "ecommerce":                [
        "ETSY",   # rationale: Etsy — two-sided online marketplace for handmade / vintage goods
        "RH",     # rationale: RH (Restoration Hardware) — luxury home furnishings (cross-listed in building_products)
        "W",      # rationale: Wayfair — online home goods retailer
        "EBAY",   # rationale: eBay — global marketplace for new + used goods + collectibles
        "CHWY",   # rationale: Chewy — pet products e-commerce (secular pet-spend tailwind)
        "CART",   # rationale: Maplebear (Instacart) — grocery e-commerce technology platform
        "CVNA",   # rationale: Carvana — used-vehicle e-commerce (fastest-growing US retail e-commerce)
        "SHOP",   # rationale: Shopify — commerce platform tools (cross-listed in vertical_saas)
        "CPNG",   # rationale: Coupang — Korean e-commerce + fulfillment (Rocket Delivery)
        "WIX",    # rationale: Wix — website + e-commerce platform for SMBs
        "DASH",   # rationale: DoorDash — local commerce / delivery platform (food + groceries + ads)
        "KMX",    # rationale: CarMax — used-car retailer (omnichannel + digital)
        "AMZU",   # rationale: 2x leveraged AMZN ETF (directional Amazon exposure)
    ],
    "travel_services":          [
        "CCL",    # rationale: Carnival — largest global cruise operator (85% of 2026 capacity booked at record prices)
        "NCLH",   # rationale: Norwegian Cruise Line — expanding Caribbean capacity 40% YoY (capacity-absorption risk)
        "RCL",    # rationale: Royal Caribbean — premium cruise operator (~2/3 of 2026 booked at record rates)
        "VIK",    # rationale: Viking Holdings — river + ocean expedition cruises (older affluent demographic)
        "BKNG",   # rationale: Booking Holdings — online travel platforms (Booking.com / Priceline / Agoda / Kayak)
        "EXPE",   # rationale: Expedia Group — online travel (Expedia / Vrbo / Hotels.com)
    ],
    "airlines":                 [
        "AAL",   # rationale: American Airlines — global network carrier (Latin America + transatlantic strength)
        "UAL",   # rationale: United Airlines — premium-cabin + international (positioned with Delta as leader)
        "JBLU",  # rationale: JetBlue — leisure-focused with TrueBlue / Mint premium cabin restructuring
        "LUV",   # rationale: Southwest — capacity-disciplined (~2% FY 2026 growth at low end of guide)
        "ALK",   # rationale: Alaska Air Group — west-coast carrier with Hawaiian integration
    ],
    "streaming_media":          [
        "SPOT",   # rationale: Spotify — audio streaming + podcasts; Premium Lite $8.99 / standard $15.99 ad-free
        "ROKU",   # rationale: Roku — connected-TV operating system + ad platform (85.5M streaming households)
        "PSKY",   # rationale: Paramount Skydance — media + entertainment post-merger
        "SPHR",   # rationale: Sphere Entertainment — Las Vegas immersive venue + MSG Networks
        "CHTR",   # rationale: Charter Communications — broadband connectivity + Spectrum video bundles
        "GDC",    # rationale: GD Culture Group — short-video / live-streaming + e-commerce platform
    ],
    "gaming_betting":           [
        "RBLX",   # rationale: Roblox — immersive gaming + social platform (heavy young-user engagement)
        "GME",    # rationale: GameStop — specialty retailer of games + collectibles (meme/treasury optionality)
        "DKNG",   # rationale: DraftKings — online sports betting + prediction markets ($55-80B GGR opportunity by 2030)
    ],
    "social_media":             [
        "SNAP",   # rationale: Snap — Snapchat camera + AR (cross-listed in ad_tech_marketing); ARPU +10% Q1 2026
        "PINS",   # rationale: Pinterest — visual search/discovery (cross-listed); 631M MAUs, 10 consec quarters DD growth
        "RDDT",   # rationale: Reddit — community discussion + ads (cross-listed); ARPU +61% in Q1 with $663M Q1 revenue
        "METU",   # rationale: 2x leveraged META ETF (directional Meta exposure)
    ],

    # ═══════════════════════════════════════════════════
    #  REAL ESTATE & PROPTECH
    # ═══════════════════════════════════════════════════
    "real_estate_proptech":     [
        "Z",      # rationale: Zillow — housing super-app strategy (search + financing + touring + agents)
        "COMP",   # rationale: Compass — residential brokerage end-to-end technology platform
        "OPEN",   # rationale: Opendoor — iBuying / "Opendoor 2.0" asset-light home-flipping platform
        "CSGP",   # rationale: CoStar Group — commercial real-estate information + analytics + listings
        "CBRE",   # rationale: CBRE — global commercial real-estate services + investment management
        "JLL",    # rationale: Jones Lang LaSalle — commercial real-estate services + capital markets
    ],

    # ═══════════════════════════════════════════════════
    #  ELECTRONIC COMPONENTS, AUTO PARTS, MISC
    # ═══════════════════════════════════════════════════
    "auto_parts_tech":          [
        "APTV",   # rationale: Aptiv — high-voltage architecture + ADAS / autonomous tech (cross-listed in ev_supply_chain/autonomous)
        "BWA",    # rationale: BorgWarner — powertrain + battery-management (cross-listed in ev_supply_chain)
        "LKQ",    # rationale: LKQ — aftermarket replacement parts distribution (collision + mechanical)
        "MOD",    # rationale: Modine — thermal management (cross-listed in datacenter_buildout / ev_supply_chain / power)
        "ALSN",   # rationale: Allison Transmission — commercial-vehicle EV transmissions (cross-listed in ev_supply_chain)
        "ST",     # rationale: Sensata — sensors / sensor-rich solutions for vehicle systems (cross-listed)
    ],
    "electronic_components":    [
        "APH",
        "VSH",    # rationale: Vishay — discrete semis + passives; book-to-bill 1.20 on AI-power demand
        "LFUS",   # rationale: Littelfuse — circuit protection + sensing components (auto + industrial)
        "VICR",   # rationale: Vicor — high-density power-modules for AI/HPC and aerospace
        "OLED",   # rationale: Universal Display — OLED IP + emitter materials (Samsung/LG royalty stream)
        "IDCC",   # rationale: InterDigital — wireless / video IP licensing (5G/6G royalties)
    ],
    "data_analytics_terminals": [
        "FDS",    # rationale: FactSet — financial data + market analytics platform
        "TRI",    # rationale: Thomson Reuters — content + technology for legal / tax / news (Westlaw + Refinitiv)
        "TYL",    # rationale: Tyler Technologies — gov data + analytics (cross-listed in vertical_saas)
        "IT",     # rationale: Gartner — business + tech research subscriptions / advisory
        "G",      # rationale: Genpact — BPO + IT services with analytics + AI consulting
        "VRSK",   # rationale: Verisk — insurance / climate / financial analytics (~$3.2B 2026 revenue)
        "TRU",    # rationale: TransUnion — global consumer credit reporting + risk analytics
        "FICO",   # rationale: Fair Isaac — credit-scoring analytics + decision-management software
    ],
    "consulting_govt":          [
        "ACN",    # rationale: Accenture — strategy + IT consulting (DOGE federal-contract reviews headwind)
        "EPAM",   # rationale: EPAM Systems — digital engineering + software services (commercial-heavy)
        "CTSH",   # rationale: Cognizant — IT services + digital transformation
        "BAH",    # rationale: Booz Allen Hamilton — federal IT + AI consulting (cutting 2.5K jobs; cross-listed in defense_tech)
        "CACI",   # rationale: CACI — defense + intel IT services (cross-listed in defense_tech)
        "LDOS",   # rationale: Leidos — federal mission IT + defense solutions (cross-listed in defense_tech)
    ],

    # ═══════════════════════════════════════════════════
    #  BIOTECH — placed last, isolated from the rest
    # ═══════════════════════════════════════════════════
    "biotech":                  [
        "RVMD", "AXSM", "CELC", "MIRM", "PTCT", "ARWR", "BBIO", "ALNY", "INSM",
        "MDGL", "PRAX", "TVTX", "MRNA", "NTLA", "CYTK", "HALO", "ABVX", "ERAS", "ROIV",
        "TEM",    # rationale: Tempus AI — clinical-data layer for oncology (cross-listed in ai_apps_platforms)
        "ORKA",   # rationale: Oruka Therapeutics — clinical-stage immunology / psoriasis biologics
        "GH",     # rationale: Guardant Health — liquid biopsy precision-oncology (cross-listed in diagnostics_tools)
        "NTRA",   # rationale: Natera — diagnostics + Signatera MRD (cross-listed in diagnostics_tools)
        "AKAN",   # rationale: Akanda — cannabis biotech (acquired 2025, status uncertain; legacy holding)
        "HIMS",   # rationale: Hims & Hers — consumer telehealth + compounded GLP-1 (more consumer than pure biotech)
        "LABU",   # rationale: Direxion 3x S&P Biotech Bull ETF (leveraged sector exposure)
        "ARKG",   # rationale: ARK Genomic Revolution ETF (active gene/biotech basket)
    ],
}


# Display labels for each theme key
THEME_LABELS = {
    "mag7":                     "MAG7 · mega-cap tech anchors",
    "ai_compute":               "AI Compute · semis",
    "ai_optics":                "AI Optics · 800G/1.6T",
    "ai_networking":            "AI Networking · DC fabric",
    "ai_connectivity":          "AI Connectivity · analog & mixed signal",
    "semiconductor_ip":         "Semiconductor IP · ARM, EDA",
    "memory_cycle":             "Memory Cycle · HBM/NAND",
    "semi_equipment":           "Semi Equipment · wafer fab cap-ex",
    "datacenter_buildout":      "Datacenter Buildout · servers, cooling, racks, fiber",
    "ai_compute_clouds":        "AI Compute Clouds · GPU-as-a-Service + miner-pivot DCs",
    "semiconductor_testing":    "Semiconductor Testing · ATE & metrology",
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
    "autonomous_mobility":      "Autonomous Mobility · self-driving + eVTOL",
    "crypto_platforms":         "Crypto Platforms & Stablecoins",
    "bitcoin_treasury":         "Bitcoin Treasury Cos",
    "crypto_miners":            "Crypto Miners",
    "drones":                   "Drones",
    "space_economy":            "Space Economy",
    "robotics_automation":      "Robotics & Automation",
    "defense_tech":             "Defense Tech",
    "critical_minerals":        "Critical Minerals & Rare Earths",
    "gold_silver_miners":       "Gold & Silver Miners",
    "industrial_metals":        "Industrial Metals · copper, steel, alum, specialty",
    "cybersecurity":            "Cybersecurity",
    "data_devops_platforms":    "Data & DevOps Platforms",
    "cloud_infra":              "Cloud Infra · edge, CDN, hosting",
    "productivity_saas":        "Productivity SaaS",
    "vertical_saas":            "Vertical SaaS",
    "ad_tech_marketing":        "Ad Tech & Marketing",
    "medtech_devices_consumer_health":  "Medtech Devices & Consumer Health",
    "diagnostics_tools":        "Diagnostics & Life Science Tools",
    "managed_care":             "Managed Care · payors & providers",
    "fintech_disruptors":       "Fintech Disruptors",
    "financial_services_specialty": "Financial Services · payments + specialty ins",
    "alt_asset_managers":       "Alt Asset Managers · PE/credit",
    "reshoring_construction":   "Reshoring & Construction",
    "electrical_grid":          "Electrical Grid & Power Equipment",
    "hvac_cooling":             "HVAC & Cooling",
    "building_products":        "Building Products & Housing Stack",
    "industrial_distribution":  "Industrial Distribution",
    "transports":               "Transports · trucking, freight, leasing, rental",
    "oil_gas_ep":               "Oil & Gas E&P · upstream + LNG",
    "oil_gas_services":         "Oil & Gas Services",
    "industrial_materials":     "Industrial Materials · chemicals, packaging, fertilizer",
    "energy_drinks_wellness":   "Energy Drinks & Wellness",
    "restaurants_fast_casual":  "Restaurants · fast casual",
    "footwear_apparel":         "Footwear & Apparel",
    "beauty":                   "Beauty",
    "discount_retail":          "Discount Retail",
    "ecommerce":                "E-commerce & DTC",
    "travel_services":          "Travel Services · cruise + OTAs",
    "airlines":                 "Airlines",
    "streaming_media":          "Streaming, Media & Cable",
    "gaming_betting":           "Gaming & Sports Betting",
    "social_media":             "Social Media",
    "real_estate_proptech":     "Real Estate & Proptech",
    "auto_parts_tech":          "Auto Parts & Tech",
    "electronic_components":    "Electronic Components",
    "data_analytics_terminals": "Data Analytics & Terminals",
    "consulting_govt":          "Consulting & Gov Services",
    "biotech":                  "Biotech · therapeutics, gene editing, GLP-1",
}


# Trader narratives — what's driving money flow per theme.
# One 1-2 sentence narrative per theme. `# source: <url>` above each entry.
# Zero confabulation: every narrative is grounded in cited reporting.
# Filled in batches as themes are researched; missing keys fall back to
# rendering without a narrative line.
THEME_NARRATIVES = {
    # source: https://finance.yahoo.com/markets/article/magnificent-7-earnings-rush-reveals-ai-spending-surge-with-hyperscaler-capex-set-to-reach-725-billion-in-2026-224901707.html
    "mag7":                     "The seven biggest US tech companies driving the bulk of S&P earnings growth; four of them (MSFT, GOOGL, AMZN, META) are guiding to roughly $725B of 2026 capex with ~75% earmarked for AI infrastructure.",

    # source: https://www.heygotrade.com/en/blog/how-hyperscaler-capex-drives-semiconductor-stock-prices/
    "ai_compute":               "GPUs and custom AI silicon at the center of the hyperscaler capex wave; NVDA data-center revenue $194B fiscal 2026, AMD data-center +57% YoY, plus the broader ecosystem of analog power, advanced packaging, memory controllers, and programmable logic feeding the same buildout.",

    # source: https://tech-insider.org/nvidia-silicon-photonics-lumentum-coherent-ai-data-center-2026/
    "ai_optics":                "800G transceivers shipping in volume and 1.6T ramping in 2H 2026; NVIDIA's $4B silicon-photonics investments in Lumentum and Coherent flagged optics as a hard capacity bottleneck for AI clusters.",

    # source: https://exoswan.com/ai-infrastructure-stocks/
    "ai_networking":            "Ethernet/InfiniBand fabric between GPU racks and across buildings; Arista raised 2026 AI guidance to $3.5B, Credo's AEC revenue ramped +272% YoY, Astera Labs entered the $20B switching market with Scorpio in early 2026.",

    # source: https://markets.financialcontent.com/stocks/article/finterra-2026-1-28-the-connectivity-powerhouse-a-deep-dive-into-astera-labs-alab-and-the-future-of-ai-fabrics
    "ai_connectivity":          "The PCIe 6 / CXL / NVLink layer moving data between GPUs, CPUs, memory, and storage inside an AI rack; Astera Labs Aries retimers, Marvell Alaska P retimers, plus the analog/mixed-signal supply for high-speed serdes.",

    # source: https://finance.yahoo.com/sectors/technology/articles/arm-holdings-doubled-2026-outperforming-192717021.html
    "semiconductor_ip":         "Royalty engines on every shipped AI chip — ARM at $4.92B FY2026 revenue with data-center royalty more than doubling YoY, plus EDA tools from Synopsys and Cadence required to design AI silicon.",

    # source: https://news.ssbcrack.com/memory-and-storage-stocks-surge-as-ai-demand-boosts-micron-sandisk-and-western-digital/
    "memory_cycle":             "HBM sold out through calendar 2026 for Nvidia Blackwell; data centers now consume over 50% of industry DRAM+NAND; Sandisk tripled YTD, Western Digital +77%, Seagate +65%.",

    # source: https://ts2.tech/en/applied-materials-stock-jumps-today-as-tsmc-lifts-2026-capex-and-barclays-upgrades-amat/
    "semi_equipment":           "Wafer-fab capex is the upstream pulse — TSMC lifted 2026 capex to $52-56B (vs $46B consensus), driving same-day 5-9% rallies in LRCX, AMAT, and KLA.",

    # source: https://www.datacenterfrontier.com/machine-learning/article/55353727/from-silicon-to-cooling-delloro-maps-the-ai-data-center-buildout
    "datacenter_buildout":      "Power, cooling, racks, servers, and integration around the GPUs — Vertiv supplies the power/cooling ecosystem, Dell carries $43B AI server backlog, Celestica builds custom hyperscaler racks, and liquid cooling became non-negotiable in 2026.",

    # source: https://sergeycyw.substack.com/p/neo-clouds-ai-infrastructures-fastest
    "ai_compute_clouds":        "GPU-as-a-service operators with multi-year hyperscaler contracts — CoreWeave $11B+ contracted at Polaris Forge 1, IREN $9.7B Microsoft deal + Nvidia partnership, Applied Digital $16B leased capacity; several pivoted from Bitcoin mining to AI compute leasing.",

    # source: https://www.lambdafin.com/articles/vistra-vs-constellation-vs-talen
    "power_demand_for_ai":      "Utilities and power-equipment names selling firm 24/7 baseload to hyperscalers — Constellation closed Calpine (55GW combined), Vistra signed a 20-yr AWS PPA up to 1,200 MW at Comanche Peak nuclear, Talen's expanded Amazon Susquehanna deal supplies up to 1,920 MW for ~$18B in revenue.",

    # source: https://markets.financialcontent.com/wral/article/marketminute-2026-1-5-the-nuclear-renaissance-nuscale-power-shares-gap-up-as-smr-technology-hits-major-milestones
    "nuclear_renaissance":      "AI power demand reviving baseload nuclear — Microsoft/Amazon/Meta signing 20-yr PPAs with existing operators, TVA planning up to 6 GW of NuScale SMRs, Oklo pursuing on-site colocation with AI data centers, BWXT noticing the NRC for a new enrichment facility.",

    # source: https://finance.yahoo.com/news/nuclear-comeback-2026-3-uranium-175000894.html
    "uranium_pure_plays":       "Pure mining/enrichment exposure to the nuclear restart — Cameco the sector blue-chip, UEC running the first new US in-situ recovery (Burke Hollow) in a decade, Centrus on enrichment, Energy Fuels at the only licensed US conventional mill.",

    # source: https://www.blockhead.co/2026/05/05/circle-surges-20-as-clarity-act-breakthrough-lifts-crypto-stocks/
    "crypto_platforms":         "Exchanges + stablecoin infrastructure riding the CLARITY Act and OCC trust-bank approvals — Coinbase $1.35B 2025 stablecoin revenue (USDC ATH market cap), Circle granted conditional OCC trust charter, stablecoin market $305B Q1 2026 projected to $3T by 2030.",

    # source: https://www.mexc.com/news/566675
    "bitcoin_treasury":         "Equity proxies for crypto via balance-sheet hoarding — MSTR holds 818,334 BTC ($54B+ cost basis) and accounted for 97.5% of net new corporate BTC purchases in early 2026; BMNR running the same playbook on Ethereum. Funding via convertible debt + perpetual preferreds.",

    # source: https://bitcoinminingstock.io/hashrate
    "crypto_miners":            "Public hashrate leaders MARA (72 EH/s), Bitdeer (65), CleanSpark (46); post-2024-halving margin pressure pushed Hut 8, CleanSpark, Cipher, TeraWulf, and Riot to add AI/HPC compute hosting alongside Bitcoin mining.",

    # source: https://www.thestreet.com/investing/drone-stocks-to-target-as-military-appetite-surges
    "drones":                   "Defense and counter-drone procurement surge — Pentagon requesting ~$54B for autonomous drones in FY2027, Anduril signed a $20B Army contract; tactical-UAS public names AVAV/KTOS/RCAT/ONDS leading the rally.",

    # source: https://www.investing.com/analysis/pentagon-goes-allin-on-ai-historic-defense-budget-fuels-defensetech-stocks-200678438
    "defense_tech":             "$839B FY2026 base defense budget + $150B Golden Dome appropriation; AI-and-autonomy primes (Kratos, AeroVironment, BWXT, Axon, Anduril) gaining share against legacy primes as procurement shifts to attritable systems.",

    # source: https://www.cnbc.com/2025/10/13/rare-earth-stocks.html
    "critical_minerals":        "China's October 2025 rare-earth export controls (suspended for 1 year until Nov 2026) re-rated Western producers — MP Materials +21%, Energy Fuels +16%, USA Rare Earth +18% on the announcement; DoD set $110/kg neodymium-praseodymium floor for MP, a 70% premium to spot.",

    # source: https://finance.yahoo.com/news/quantum-computing-set-scale-2026-180000840.html
    "quantum_computing":        "Speculative leg on quantum hardware — IonQ 2025 revenue +202% YoY guiding $225-245M for 2026 with $370M backlog; Rigetti's $590M cash funds 108-qubit + 150-qubit deployments through 2026.",

    # source: https://www.gurufocus.com/news/2607271/applovin-app-and-palantir-pltr-surge-as-ai-applications-drive-market-optimism
    "ai_apps_platforms":        "Enterprise + consumer AI software riding on top of the GPU stack — Palantir AIP across gov + enterprise, AppLovin AXON drove Q4 2025 revenue +66% YoY at 84% adj EBITDA margin, Tempus Q4 2025 revenue +83% on clinical-data layer for oncology.",

    # source: https://semiengineering.com/hbm-shifts-testing-left-to-preserve-ai-chip-yield/
    "semiconductor_testing":    "Teradyne + Advantest control ~80% of ATE; HBM4/HBM5 testing costs rising to 10-15% of chip cost (vs ~5% for traditional SoCs); Aehr Test backlog $50.9M driven by silicon-photonics + wafer-level burn-in for AI data centers.",

    # source: https://www.sec.gov/Archives/edgar/data/0001780312/000119312526216946/asts-ex99_1.htm
    "space_economy":            "Direct-to-cell broadband, proliferated LEO + lunar landers, and commercial launch — AST SpaceMobile targeting 45 satellites in orbit by end of 2026 with FCC commercial authorization, Rocket Lab providing launch + spacecraft to defense/commercial.",

    # source: https://www.automate.org/videos/symbotic-system-overview
    "robotics_automation":      "Symbotic Q2 FY2026 revenue $676M +23% YoY for AI-enabled warehouse robots; industrial automation and humanoid-robot platforms commercializing through 2026 on labor scarcity + reshoring.",

    # source: https://finance.yahoo.com/sectors/technology/articles/tesla-just-beat-byd-ev-143027681.html
    "ev_makers":                "BYD leads global EV share at 17.1% in March 2026 vs Tesla at 8.9%, but Tesla retook Q1 with 358K deliveries vs BYD's 310K; Rivian R2 SUV launching 2026, Lucid focused on Midsize + first robotaxi deployments.",

    # source: https://www.borgwarner.com/newsroom/press-releases/2026/02/11/borgwarner-expands-series-production-battery-management-system-program-with-a-global-oem
    "ev_supply_chain":          "Powertrain + battery-management content per vehicle still growing — BorgWarner reaffirmed 2026 outlook with new global-OEM BMS program, Albemarle still digesting the lithium market reset, Aptiv high-voltage architectures into next-gen EV platforms.",

    # source: https://www.fool.com/investing/how-to-invest/stocks/how-to-invest-in-waymo-stock/
    "autonomous_mobility":      "Waymo expanding robotaxi to Atlanta/Miami/DC + Tokyo/London in 2026 ($16B raise at $126B post-money); Mobileye targeting 100K self-driving vehicles by 2033; Aurora running driverless trucks Dallas↔Houston commercially.",

    # source: https://markets.financialcontent.com/wral/article/marketminute-2026-3-2-solar-giants-guidance-cliff-first-solar-shares-crater-18-on-weak-2026-outlook-and-policy-headwinds
    "solar":                    "Mixed 2026 — First Solar backlog runs through 2029 with IRA Section 48E 10-pt domestic-content bonus, but new 15% component tariffs and federal permit freeze cost $135M to FSLR 2026; Section 25D residential credit expired Dec 31 2025.",

    # source: https://www.energy-storage.news/fluence-expects-to-meet-feoc-compliance-by-regulatory-deadlines-offers-2026-guidance/
    "energy_storage":           "Fluence guiding $3.2-3.6B FY2026 (~85% in backlog) on grid-scale BESS + AI-powered bidding software across ~50 markets; Eos scaling zinc long-duration storage with Cerberus joint-venture IPP.",

    # source: https://www.sec.gov/Archives/edgar/data/0001664703/000162828026027913/ex991_q126financialresults.htm
    "fuel_cells_hydrogen":      "On-site solid-oxide fuel cells from Bloom Energy expanding into data centers + semiconductor fabs as firm 24/7 power; pure-play hydrogen names (Plug, FuelCell) remain earlier-stage, tied to 45V production tax credit and large-customer adoption.",

    # source: https://parameter.io/may-2026-cybersecurity-stocks-top-5-picks-including-crowdstrike-crwd-and-datadog-ddog/
    "cybersecurity":            "AI deployment expands attack surface + machine identities + data-in-motion, creating pressure to consolidate security stacks — CRWD Falcon, PANW platform play, FTNT Q1 billings +31% YoY at $2.09B, ZS / OKTA each capturing different parts of the consolidation.",

    # source: https://www.tikr.com/blog/datadog-vs-snowflake-which-cloud-data-stock-is-the-better-growth-play
    "data_devops_platforms":    "Consumption-based platforms benefit from AI workload growth — Snowflake re-accelerated to 29-32% YoY revenue growth, MongoDB Atlas accelerating, Datadog +27.7% in fiscal 2025 on observability for AI/security/hybrid.",

    # source: https://siliconangle.com/2026/01/22/nutanix-enterprise-ai-infrastructure-shift-nextconf/
    "cloud_infra":              "Edge + CDN + private cloud — Cloudflare programmable edge (Workers, edge functions), Nutanix Cloud Platform bridging on-prem to public cloud (GPT-in-a-Box + Agentic AI in 2H 2026), Rackspace FAIR multi-cloud AI hosting.",

    # source: https://www.pymnts.com/artificial-intelligence-2/2026/servicenow-sap-and-workday-make-ai-agents-pay-to-play/
    "productivity_saas":        "Enterprise SaaS pivoting to AI-agent monetization — ServiceNow Action Fabric (Knowledge 2026), Workday Agent System of Record + Sana; Feb 2026 'SaaSpocalypse' wiped ~$285B after Anthropic Cowork plugins (HUBS -51%, MNDY -44%, NOW -36% peak-to-trough).",

    # source: https://tech-insider.org/the-rise-of-vertical-saas-why-industry-specific-software-is-winning/
    "vertical_saas":            "Industry-specific SaaS reports 31% median growth (vs 28% horizontal) with 3x retention — Veeva (life sciences, $35B+ mkt cap), Toast (85K+ restaurants), Guidewire (450+ insurers), Shopify (commerce); embedded finance accelerating.",

    # source: https://digiday.com/marketing/ad-tech-briefing-applovins-ai-fueled-surge-and-the-trade-desks-stumble-show-where-investors-are-placing-their-bets/
    "ad_tech_marketing":        "AppLovin AXON +68% YoY at $1.4B Q4 revenue, Trade Desk's Ventura CTV initiative, Pinterest opening Media Network Connect to non-endemic advertisers — CTV + retail-media + AI signal stacking are the 2026 catalysts.",

    # source: https://247wallst.com/investing/2026/04/15/upstart-soars-14-affirm-climbs-7-two-fintech-disruptors-signal-the-sector-may-be-turning-a-corner/
    "fintech_disruptors":       "Digital-finance challengers turning the corner — SoFi Q1 2026 revenue +41% YoY ($1.10B) with record $12.18B originations +68%; Chime $647M Q1 revenue with 10.2M active members; Upstart Q4 revenue +31% YoY on AI lending models.",

    # source: https://www.insurancebusinessmag.com/us/news/breaking-news/ryan-specialty-swings-to-q1-profit-as-eands-tailwinds-fade-573772.aspx
    "financial_services_specialty": "E&S insurance market crossed $100B in 2025 (+7.8%) — Ryan Specialty Q1 2026 revenue +15.2% YoY at $795M, Kinsale's pure E&S underwriting compounding; payments names (CPAY, GPN) on processor consolidation.",

    # source: https://www.morningstar.com/stocks/why-alts-manager-stocks-are-getting-hit-hard
    "alt_asset_managers":       "Private-credit-heavy alts under pressure in 2026 — Apollo (86% PC of fee-earning AUM), Ares (66%), Blue Owl (53%) all down double-digits YTD; Carlyle's more balanced book down only ~2.4%.",

    # source: https://www.marketscreener.com/news/vrsk-verisk-analytics-expects-2026-revenue-range-3-19b-3-24b-vs-factset-est-of-3-21b-ce7f59d3d081f52c
    "data_analytics_terminals": "Subscription financial-data + analytics franchises — FactSet integrated platform with AI insights, Verisk guiding $3.19-3.24B 2026 revenue on insurance analytics, Gartner research subscriptions, FICO/TransUnion credit analytics.",

    # source: https://www.sec.gov/Archives/edgar/data/0001323404/000119312526212446/d91444dex991.htm
    "gold_silver_miners":       "Royalty/streaming + producer leverage to precious-metals price — Wheaton expanded silver stream on Antamina (BHP) Feb 2026, added Jervois (KGL Resources) Apr 2026; sector beneficiary of debasement / safe-haven flows.",

    # source: https://www.ainvest.com/news/trump-tariff-expansion-impact-steel-aluminum-sectors-strategic-financial-implications-producers-cleveland-cliffs-2508/
    "industrial_metals":        "Section 232 tariffs on steel/aluminum hiked 25%→50% in 2025 + extended to 400+ derivative products; Cleveland-Cliffs Q1 2026 HRC avg $980/ton (+24% YoY), $4.8B steelmaking revenue; Freeport / Southern Copper geared to copper electrification demand.",

    # source: https://money.usnews.com/investing/articles/diabetes-and-weight-loss-drug-stocks-with-big-potential
    "biotech":                  "GLP-1 obesity drugs mainstream and pipelines expanding; gene-editing readouts (Intellia, Verve, CRISPR), MASH NASH (Madrigal), rare disease (Alnylam, BridgeBio), oncology (Revolution Medicines, Arrowhead) — pipeline-driven cohort with binary catalysts.",

    # source: https://www.modernhealthcare.com/medical-devices/mh-medical-devices-2026-dexcom-ge-healthcare-philips/
    "medtech_devices_consumer_health":  "Diabetes care + consumer health — Dexcom guiding 2026 revenue $5.16-5.25B (+11-13%) with G7 15-Day expansion, Insulet pivotal Type 2 trial in 2026, Glaukos highest medtech R&D intensity; consumer health pulls in Hims & Hers / Zoetis.",

    # source: https://www.stocktitan.net/sec-filings/NTRA/8-k-natera-inc-reports-material-event-df549edda1b1.html
    "diagnostics_tools":        "Liquid biopsy + life-science tools rerating — Natera Q1 2026 revenue $696.6M (+38.8%) with clinical oncology volumes +54.4% YoY, MRD adoption (Signatera Medicare coverage) and MCED (Guardant Shield, Exact Cancerguard) hitting clinical inflection.",

    # source: https://www.fiercehealthcare.com/regulatory/2026-ma-star-ratings-aetna-humana-see-score-decline-unitedhealthcare-improves
    "managed_care":             "2026 Medicare Advantage Star Ratings stabilized after multi-year declines — Humana 3.61 avg (declining MA-Star membership share), Centene 3.39 (up from 3.14, 20% of members in 4+ star plans vs 1% prior year); margin recovery story.",

    # source: https://www.fedmanager.com/news/trump-administration-pushes-agencies-to-cut-consulting-contracts
    "consulting_govt":          "Federal consulting crackdown — Hegseth terminated $5.1B DoD consulting contracts, DHA cuts saving $1.8B from Accenture/Deloitte/Booz Allen; Booz Allen cutting 2,500 jobs, fiscal 2026 revenue projected -3%; Accenture federal services losing DOGE-reviewed contracts.",

    # source: https://www.ad-hoc-news.de/boerse/news/ueberblick/amphenol-stock-us0320951017-analysts-targets-diverge-as-connectivity/69339273
    "electronic_components":    "Vishay Q4 book-to-bill 1.20 with orders at 3-yr high on AI-power + industrial demand; Amphenol benefiting from connector demand across AI/auto/aerospace; selective SKU-specific component supply tightness in auto + EV + industrial.",

    # source: https://www.tikr.com/blog/10-auto-supplier-stocks-riding-the-ev-transition
    "auto_parts_tech":          "EV + ADAS content per vehicle still rising — Aptiv in 18 of 20 top-selling US models in 2025 with high-voltage architecture + ADAS, BorgWarner expanding battery-management programs, Modine thermal for EV cooling.",

    # source: https://www.precedenceresearch.com/proptech-market
    "real_estate_proptech":     "Proptech market $54.66B → $209B by 2035 (CAGR 16%); Zillow housing super-app strategy, Opendoor 2.0 asset-light iBuying, CoStar enterprise focus; ROAD Act (Senate Mar 2026) restricts institutional SFR buying for 15 years.",

    # source: https://finance.yahoo.com/markets/stocks/articles/comfort-systems-vs-quanta-infrastructure-142100352.html
    "reshoring_construction":   "Record contractor backlogs on AI-DC + semi-plant + reshoring construction — Quanta $48.5B backlog, MasTec $19B (+13% sequential), Comfort Systems $12.45B (vs $6.89B YoY; advanced tech = 56% of revenue).",

    # source: https://www.powermag.com/transformers-in-2026-shortage-scramble-or-self-inflicted-crisis/
    "electrical_grid":          "Transformer / breaker lead times stretched to 4 years; >50% of planned 2026 US data centers expected delayed — Eaton expanding $1.2B capacity, Vertiv guiding $13.25-13.75B 2026 (+27-29% organic), Powell switchgear sold out.",

    # source: https://www.aaon.com/news/aaon-awarded-liquid-cooling-data-center-orders-totaling-approximately-174.5-million
    "hvac_cooling":             "Datacenter cooling pulled HVAC makers into AI-adjacent backlog — Modine $180M AI-DC cooling orders + new Wisconsin plant, AAON $174.5M liquid-cooling order for one DC customer; Watsco distribution layer for residential + light commercial.",

    # source: https://www.investing.com/news/company-news/builders-firstsource-q1-2026-slides-earnings-miss-amid-housing-slump-93CH-4650421
    "building_products":        "Soft 2026 housing — US single-family starts -6.5% YoY in January, Builders FirstSource lowered 2026 guide to $14.6-15.6B; R&R spending the bright spot (mid-single growth 2026, high-single 2027).",

    # source: https://www.inddist.com/earnings/news/22966161/wesco-reports-record-first-quarter
    "industrial_distribution":  "Data-center-driven distribution boom — Wesco DC sales +70% YoY now ~25% of total with record backlog, Arrow Electronics Q1 revenue $9.5B +39% on early cyclical recovery.",

    # source: https://finance.yahoo.com/markets/stocks/articles/zacks-industry-outlook-highlights-old-094000240.html
    "transports":               "Trucking still in recession (Zacks Industry Rank #173/244) — ODFL Feb revenue/day -3.3% (LTL tons -6.8%), XPO first YoY tonnage gain in 18+ months (+0.2%); demand encouragingly improved through Q1.",

    # source: https://markets.financialcontent.com/stocks/article/marketminute-2026-3-16-energy-giant-texas-pacific-land-corp-skyrockets-75-ytd-as-permian-powerhouse-evolves-into-ai-infrastructure-play
    "oil_gas_ep":               "Range-bound oil price + Permian growth — Texas Pacific Land +75% YTD on royalty + water + AI-DC-land thesis, Exxon guiding +12.5% Permian production for 2026; ~$70B US upstream assets on the market.",

    # source: https://www.sec.gov/Archives/edgar/data/0001694028/000169402826000021/lbrt_3312026.htm
    "oil_gas_services":         "NAM completions demand stabilized after softening — Liberty Energy scaling power-infra platform alongside frac, flat oil production targets but modest gas-directed growth; range-bound oil keeps service spend disciplined.",

    # source: https://247wallst.com/investing/2026/03/09/wall-street-just-sent-a-clear-signal-on-dow-and-lyondellbasell-heres-what-it-means/
    "industrial_materials":     "Chemicals deep restructuring — Dow cutting 4,500 jobs ($2B savings), LyondellBasell trimmed 2026 capex $300M, Celanese earnings -51% YoY ($1B divestiture target); NAM ethane cost advantage on Middle-East supply disruption.",

    # source: https://www.sec.gov/Archives/edgar/data/0001482981/000148298126000118/coco-20260331xexx991pressr.htm
    "energy_drinks_wellness":   "Functional-beverage growth pockets — Vita Coco Q1 2026 net sales $180M (+37%, coconut water +42%) raising 2026 guide to $720-735M; CELH energy-drink momentum + COKE bottling exposure.",

    # source: https://www.ainvest.com/news/dutch-bros-cava-restaurant-stock-offers-stronger-2026-growth-catalyst-2512/
    "restaurants_fast_casual":  "Disciplined unit-growth fast-casual standouts — Dutch Bros Q1 2026 revenue +31% YoY with 8.3% same-shop sales growth and 41 new shops opened; CAVA at 415 restaurants with comps pacing above 3-5% guide; Wingstop / Sweetgreen also on growth list.",

    # source: https://www.stocktitan.net/sec-filings/CROX/8-k-crocs-inc-reports-material-event-f9136f79aed8.html
    "footwear_apparel":         "DTC + brand momentum — On Holding reiterated FY 23%+ constant-currency net sales growth (DTC + APAC + apparel leading), Crocs DTC +12.1% (Crocs brand +12.9%, HEYDUDE +8.6%), Birkenstock 59% gross margin + 3.13 current ratio.",

    # source: https://www.investing.com/news/stock-market-news/rj-favours-beauty-stocks-estee-lauder-elf-and-ulta-in-2026-amid-slow-consumption-4457803
    "beauty":                   "Prestige beauty bifurcation — Estée Lauder navigating ~$100M tariff drag with 'Beauty Reimagined' opex reset (Q1 organic +3%, op margin +300bps to 7.3%); e.l.f. Beauty 27 consecutive quarters of net sales growth on affordable trend-led products.",

    # source: https://247wallst.com/investing/2026/03/16/dollar-tree-vs-dollar-general-which-discount-retailer-delivers-the-smarter-return-for-your-portfolio/
    "discount_retail":          "Trade-down bifurcation — Dollar Tree 3.0 multi-price now ~5,300 stores attracting 3M new households (60% from $100K+ income brackets); Dollar General SSS +4.3% but losing trade-down battle to more diversified players.",

    # source: https://www.fool.com/investing/stock-market/market-sectors/consumer-discretionary/top-ecommerce-companies/
    "ecommerce":                "Global e-commerce reaching $6.88T in 2026 (17.5% of total retail) — Amazon adding 0.4 pts share to ~38%, Carvana fastest-growing US retail e-commerce expecting sequential growth Q1 2026, Chewy benefiting from pet-product secular growth.",

    # source: https://247wallst.com/investing/2026/05/20/carnival-jumps-9-norwegian-cruise-line-soars-11-why-royal-caribbean-isnt-joining-the-cruise-party/
    "travel_services":          "Cruise demand record — Carnival 85% of 2026 capacity booked at record prices, Royal Caribbean ~2/3 booked at record rates, 34 new ships on order across top 5 cruise lines (~150K new berths); WTI spike to $111 a fuel-cost headwind.",

    # source: https://www.fool.com/investing/stock-market/market-sectors/industrials/airline-stocks/
    "airlines":                 "Capacity discipline + premium-cabin strategy — Southwest tightening 2026 capacity growth to ~2% (low end of 2-3% guide), Delta margin expansion on brand differentiation + premium-cabin strategy, fuel + geopolitics adding volatility.",

    # source: https://advertising.roku.com/2026-predictions
    "streaming_media":          "Ad-supported tiers absorbing the price hikes — ad-content now ~74% of all TV viewing, Roku connecting 85.5M streaming households (32B streaming hours Q3 2025); Netflix expanding into live sports (MLB Opening Night, Home Run Derby, WWE Raw).",

    # source: https://www.benzinga.com/analyst-stock-ratings/analyst-color/26/01/50075278/draftkings-flutter-bofa-sees-online-betting-driving-gaming-stocks-into-2026
    "gaming_betting":           "DraftKings Q1 2026 revenue $1.6B (+17% YoY), guiding 2026 revenue $6.5-6.9B + adj EBITDA $700-900M, targeting $55-80B GGR opportunity by 2030; Kalshi / Polymarket prediction-market traffic projected 445B contracts in 2026 (vs 95B in 2025).",

    # source: https://www.citybiz.co/article/846441/reddit-outpaces-social-media-rivals-in-revenue-per-user-growth/
    "social_media":             "Reddit Q1 2026 revenue +69% YoY ($663M) with US ARPU +61% to $9.20 (7 consecutive quarters of 60%+ growth); Pinterest Q1 revenue >$1B (+18%) with 631M MAUs (10th consecutive quarter of double-digit user growth).",
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
    # Added 2026-05-21 from TC2000 watchlist (399 names; theme placements to follow)
    "AAP", "ABCL", "ABG", "ACHC", "ACMR", "ADEA", "ADMA", "ADTN", "AEHL", "AEO",
    "AESI", "AI", "AIIO", "AIR", "ALGT", "ALKS", "ALM", "ALMU", "AMBA", "AMG",
    "AMR", "AMSC", "AMTM", "AN", "ANAB", "ANF", "AOSL", "APGE", "APPF", "ARCB",
    "AROC", "ARQT", "ARRY", "ASAN", "ASH", "ASO", "ASST", "ATEC", "ATMU", "ATRO",
    "AUGO", "AVEX", "AVTR", "AVTX", "AXGN", "BAND", "BC", "BCO", "BCRX", "BDC",
    "BEAM", "BELFB", "BEPC", "BFH", "BHE", "BILL", "BIPC", "BIRK", "BLDP", "BLLN",
    "BLSH", "BMI", "BORR", "BOX", "BRBR", "BRKR", "BRZE", "BSY", "BTBT", "BTU",
    "BULL", "BW", "BYND", "BZ", "CAI", "CAKE", "CALX", "CALY", "CARG", "CC",
    "CCC", "CECO", "CENX", "CGON", "CHDN", "CHEF", "CHH", "CMPS", "CNR", "CNXC",
    "COGT", "COLD", "CORT", "COUR", "CPA", "CPRI", "CRI", "CRK", "CRM", "CRNX",
    "CRSP", "CSIQ", "CSTM", "CTRI", "CVCO", "CVLT", "CWST", "DDS", "DIOD", "DK",
    "DLB", "DNTH", "DXC", "EEFT", "EFOR", "EME", "ENS", "ENVX", "EQPT", "ERIE",
    "ESAB", "ESE", "ESTC", "ETOR", "EWTX", "EXK", "EXLS", "EXTR", "EYE", "FCN",
    "FIGS", "FIVN", "FLO", "FMC", "FOUR", "FRMI", "FRPT", "FRSH", "FSM", "GLBE",
    "GLOB", "GPGI", "GPI", "GPK", "GT", "GTM", "GTX", "GXO", "HAE", "HAO",
    "HCC", "HII", "HLNE", "HNGE", "HOG", "HP", "HQY", "HRI", "HTZ", "HUN",
    "HXL", "HYMC", "IBRX", "IDXX", "IMAX", "IMVT", "INBX", "INDV", "INSP", "INSW",
    "INTR", "IOVA", "IPGP", "IRTC", "ITGR", "ITRI", "JBS", "JBTM", "JXN", "KAI",
    "KALU", "KBR", "KD", "KLAR", "KLIC", "KMT", "KNF", "KNSA", "KOPN", "KOS",
    "KRYS", "KSS", "KTB", "KYMR", "LAC", "LASR", "LAZ", "LBRDK", "LC", "LEGN",
    "LFST", "LGND", "LION", "LIVN", "LOAR", "LOPE", "LPTH", "LPX", "LQDA", "LRN",
    "M", "MAIR", "MANE", "MANH", "MATX", "MBLY", "MC", "MEOH", "MIAX", "MIR",
    "MMS", "MMSI", "MMYT", "MORN", "MRCY", "MRX", "MSGS", "MTRN", "MUR", "MWH",
    "MZTI", "NAMS", "NAVN", "NCNO", "NE", "NESR", "NEU", "NKTR", "NN", "NNE",
    "NOVT", "NPO", "NSIT", "NUVL", "NVAX", "NVST", "NXE", "NXST", "OLN", "OSIS",
    "OTEX", "PARR", "PATK", "PBH", "PBI", "PCT", "PCTY", "PCVX", "PDFS", "PEGA",
    "PENN", "PGY", "PI", "PII", "PLPC", "PLXS", "POWI", "PPC", "PPTA", "PRCT",
    "PRKS", "PRM", "PRMB", "PSIX", "PSN", "PTEN", "PTGX", "PTON", "PUMP", "PURR",
    "PVH", "PZZA", "QLYS", "QTWO", "QUCY", "QURE", "RAL", "RARE", "RDNT", "REAL",
    "REZI", "RHI", "RLAY", "RNG", "ROAD", "ROG", "RSI", "RVI", "RXO", "RXRX",
    "RYTM", "SAM", "SATL", "SBET", "SEB", "SEZL", "SGHC", "SGML", "SHLS", "SHOO",
    "SIDU", "SIG", "SIRI", "SKY", "SKYT", "SLG", "SLM", "SLNH", "SLS", "SMG",
    "SNEX", "SRAD", "SRPT", "SRRK", "SSRM", "STEP", "STNE", "STUB", "SVM", "SXI",
    "SXT", "SYRE", "TARS", "TDC", "TDIC", "TDS", "TDW", "TENB", "TEX", "TGTX",
    "THO", "THR", "TIGO", "TNDM", "TNGX", "TPC", "TREX", "TRMB", "TRT", "TTAN",
    "TWST", "TXG", "UAA", "UFPT", "ULS", "UMAC", "UPWK", "URBN", "UWMC", "VAL",
    "VC", "VCX", "VECO", "VELO", "VGNT", "VIST", "VITL", "VKTX", "VNT", "VOYG",
    "VRDN", "VRNS", "VSCO", "VSEC", "VSNT", "VVV", "VVX", "WAY", "WEN", "WERN",
    "WEX", "WGS", "WK", "WLDN", "WMG", "WOK", "WRBY", "WSC", "WSM", "WT",
    "WU", "WYFI", "XMTR", "XP", "XRAY", "YETI", "YOU", "YSS", "ZG",
    # Added 2026-05-21 for MAG7 theme
    "GOOGL", "META", "AMZN", "AAPL", "MSFT",
    # Added 2026-05-21 from TC2000 ETF watchlist (filtered: 23 of 116;
    # inverse + redundant-leveraged + no-existing-theme-fit removed)
    "SOXL", "USD", "XSD", "PSI", "TSMX",       # semis -> ai_compute
    "NUGT", "JNUG", "GDXU", "SIL", "SILJ",     # gold/silver -> gold_silver_miners
    "URA", "URNM",                             # uranium -> uranium_pure_plays
    "NLR",                                     # nuclear -> nuclear_renaissance
    "DRAM",                                    # memory -> memory_cycle
    "NASA", "UFO",                             # space -> space_economy
    "LABU", "ARKG",                            # biotech/genomics -> biotech
    "NAIL",                                    # homebuilders -> building_products
    "AMZU",                                    # AMZN proxy -> ecommerce
    "GGLL",                                    # GOOGL proxy -> ad_tech_marketing
    "METU",                                    # META proxy -> social_media
    "MSFU",                                    # MSFT proxy -> cloud_infra
]
