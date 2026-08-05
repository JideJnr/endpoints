REPLACEMENTS = {
    # suffixes to strip
    " fc": "", " cf": "", " sc": "", " ac": "", " bc": "",
    " afc": "", " fk": "", " sk": "", " bk": "",

    # english clubs
    "manchester city": "man city",
    "manchester united": "man utd",
    "nottingham forest": "nott forest",
    "tottenham hotspur": "tottenham",
    "wolverhampton": "wolves",
    "newcastle united": "newcastle",
    "brighton & hove albion": "brighton",
    "brighton and hove albion": "brighton",
    "west bromwich albion": "west brom",
    "queens park rangers": "qpr",
    "sheffield united": "sheff utd",
    "sheffield wednesday": "sheff wed",
    "leicester city": "leicester",

    # spanish clubs
    "atletico de madrid": "atletico madrid",
    "real betis balompie": "betis",
    "real betis": "betis",
    "deportivo alaves": "alaves",
    "rayo vallecano": "rayo",

    # german clubs
    "borussia moenchengladbach": "gladbach",
    "borussia mgladbach": "gladbach",
    "borussia mｴgladbach": "gladbach",
    "bayer 04 leverkusen": "leverkusen",
    "bayer leverkusen": "leverkusen",
    "rb leipzig": "leipzig",
    "eintracht frankfurt": "frankfurt",
    "1. fc nuremberg": "nuremberg",
    "1. fc koln": "cologne",
    "fc st. pauli": "st pauli",

    # italian clubs
    "ac milan": "milan",
    "inter milan": "inter",
    "fc internazionale": "inter",
    "ss lazio": "lazio",
    "as roma": "roma",
    "ssc napoli": "napoli",
    "juventus fc": "juventus",

    # french clubs
    "paris saint-germain": "psg",
    "paris sg": "psg",
    "olympique de marseille": "marseille",
    "olympique lyonnais": "lyon",
    "stade rennais": "rennes",
    "as monaco": "monaco",

    # portuguese clubs
    "sporting cp": "sporting",
    "vitoria sc guimaraes": "vitoria guimaraes",
    "vitoria sc": "vitoria guimaraes",

    # nordic clubs
    "bodoe/glimt": "bodo glimt",
    "bodø/glimt": "bodo glimt",
    "fc midtjylland": "midtjylland",

    # south american
    "ca river plate": "river plate",
    "ca boca juniors": "boca juniors",
    "se palmeiras": "palmeiras",
    "santos fc": "santos",
    "sao paulo fc": "sao paulo",
    "cr flamengo": "flamengo",
    "ca talleres de cordoba": "talleres",

    # common abbreviations sportybet uses
    "man city": "man city",
    "man utd": "man utd",
    "psg": "psg",
}


def normalise(name: str) -> str:
    text = (name or "").lower().strip()
    for source, target in REPLACEMENTS.items():
        text = text.replace(source, target)
    return " ".join(text.split())
