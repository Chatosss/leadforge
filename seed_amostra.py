"""seed_amostra.py — Gera ~26 clínicas de exemplo para desenvolvimento/piloto."""

import sqlite3
from pathlib import Path

import config
import ingest as ingest_mod

SEED_DB = config.DATA_DIR / "seed_source.db"

# (nome, categoria, avaliacao, total, telefone, website, instagram, endereco, posicao, tag)
CLINICAS = [
    # ── Odontologia ──────────────────────────────────────────────────────────
    ("Sorriso Perfeito Odontologia", "Clínica odontológica", "3,9", "287",
     "(21) 3555-1010", "", "", "Rua Barata Ribeiro, 200 - Copacabana, Rio de Janeiro", 12, "ativa-falhas"),
    ("OdontoCenter Ipanema", "Dentista", "4,2", "156",
     "(21) 3555-2020", "", "@odontocenter", "Rua Visconde de Pirajá, 550 - Ipanema, Rio de Janeiro", 8, "ativa-falhas"),
    ("Clínica Dental Premium", "Clínica odontológica", "4,9", "512",
     "(21) 3555-3030", "http://dentalpremium.com.br", "@dentalpremium", "Av. das Américas, 700 - Barra da Tijuca, Rio de Janeiro", 1, "perfeita"),
    ("Dr. João Dentista", "Dentista", "", "2",
     "", "", "", "Rua do Catete, 90 - Catete, Rio de Janeiro", 15, "fantasma"),
    ("Implante & Cia", "Clínica odontológica", "4,4", "98",
     "(21) 3555-4040", "http://implantecia.com", "", "Rua Dias da Rocha, 30 - Copacabana, Rio de Janeiro", 6, "media-alta"),
    ("Ortodontia Moderna", "Ortodontia", "4,0", "203",
     "(21) 3555-5050", "", "@ortomoderna", "Rua Farme de Amoedo, 45 - Ipanema, Rio de Janeiro", 10, "ativa-falhas"),
    ("Consultório Odontológico Silva", "Dentista", "4,6", "34",
     "(21) 3555-6060", "", "", "Rua Marquês de Abrantes, 12 - Flamengo, Rio de Janeiro", 18, "media"),
    ("Clínica Dental Popular", "Clínica odontológica", "4,3", "58",
     "(21) 3555-7070", "", "", "Rua Uruguaiana, 88 - Centro, Rio de Janeiro", 22, "media-alta"),
    ("Sorridents Centro", "Clínica odontológica", "4,1", "265",
     "(21) 3555-8080", "", "", "Av. Rio Branco, 150 - Centro, Rio de Janeiro", 17, "ativa-falhas"),
    ("Odonto Excellence", "Clínica odontológica", "4,7", "410",
     "(21) 3555-9090", "http://odontoexcellence.com.br", "@odontoexcellence", "Av. Ataulfo de Paiva, 300 - Leblon, Rio de Janeiro", 4, "quase-perfeita"),
    ("Odontologia Familiar Bairro", "Dentista", "3,5", "76",
     "(21) 3555-1111", "", "", "Rua Conde de Bonfim, 400 - Tijuca, Rio de Janeiro", 19, "ativa-falhas"),

    # ── Estética ─────────────────────────────────────────────────────────────
    ("Bella Estética Avançada", "Clínica de estética", "4,1", "176",
     "(21) 3555-1212", "", "@bellaestetica", "Rua Garcia d'Ávila, 60 - Ipanema, Rio de Janeiro", 9, "ativa-falhas"),
    ("Estúdio da Beleza", "Clínica de estética", "3,7", "89",
     "(21) 3555-1313", "", "", "Rua Voluntários da Pátria, 190 - Botafogo, Rio de Janeiro", 14, "ativa-falhas"),
    ("Harmonização VIP", "Harmonização facial", "4,8", "334",
     "(21) 3555-1414", "http://harmovip.com.br", "@harmovip", "Av. das Américas, 500 - Barra da Tijuca, Rio de Janeiro", 2, "perfeita"),
    ("Clínica Skin Care", "Clínica de estética", "4,3", "142",
     "(21) 3555-1515", "http://skincare.com.br", "", "Rua Almirante Tamandaré, 20 - Flamengo, Rio de Janeiro", 7, "media-alta"),
    ("Espaço Estético Renove", "Clínica de estética", "4,0", "67",
     "(21) 3555-1616", "", "@renoveestetica", "Rua São Clemente, 100 - Botafogo, Rio de Janeiro", 11, "ativa-falhas"),
    ("Dermato Estética Center", "Dermatologia", "4,5", "221",
     "(21) 3555-1717", "http://dermatocenter.com.br", "@dermatocenter", "Rua Sá Ferreira, 40 - Copacabana, Rio de Janeiro", 5, "media"),
    ("Beauty Lounge", "Clínica de estética", "", "0",
     "", "", "", "Rua Miguel Lemos, 15 - Copacabana, Rio de Janeiro", 20, "fantasma"),
    ("Estética Facial & Corporal", "Clínica de estética", "3,8", "118",
     "(21) 3555-1818", "", "", "Rua Bento Lisboa, 80 - Catete, Rio de Janeiro", 13, "ativa-falhas"),
    ("Estética Glow Up", "Clínica de estética", "3,9", "132",
     "(21) 3555-1919", "", "@glowupestetica", "Rua Jardim Botânico, 600 - Jardim Botânico, Rio de Janeiro", 6, "ativa-falhas"),
    ("Lumière Estética", "Harmonização facial", "5,0", "88",
     "(21) 3555-2121", "http://lumiereestetica.com.br", "@lumiereestetica", "Rua Dias Ferreira, 50 - Leblon, Rio de Janeiro", 1, "perfeita"),

    # ── Fisioterapia ─────────────────────────────────────────────────────────
    ("FisioVida Reabilitação", "Clínica de fisioterapia", "4,2", "94",
     "(21) 3555-2222", "", "@fisiovida", "Rua Real Grandeza, 200 - Botafogo, Rio de Janeiro", 8, "ativa-falhas"),
    ("Studio Pilates Equilíbrio", "Pilates", "4,9", "205",
     "(21) 3555-2323", "http://pilatesequilibrio.com.br", "@pilatesequilibrio", "Rua Humaitá, 100 - Humaitá, Rio de Janeiro", 3, "perfeita"),
    ("Vida & Saúde Fisioterapia", "Clínica de fisioterapia", "4,0", "51",
     "(21) 3555-2424", "", "", "Rua Haddock Lobo, 300 - Tijuca, Rio de Janeiro", 12, "media"),

    # ── Veterinária ──────────────────────────────────────────────────────────
    ("PetSaúde Clínica Veterinária", "Clínica veterinária", "4,1", "312",
     "(21) 3555-2525", "", "@petsaude", "Rua Voluntários da Pátria, 400 - Botafogo, Rio de Janeiro", 10, "ativa-falhas"),
    ("Clínica Animal Amigo", "Clínica veterinária", "3,6", "45",
     "(21) 3555-2626", "", "", "Rua Barão de Mesquita, 500 - Tijuca, Rio de Janeiro", 16, "ativa-falhas"),
]


def _criar_source():
    """(Re)cria o banco de origem shaped como `businesses` + coluna posicao."""
    if SEED_DB.exists():
        SEED_DB.unlink()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SEED_DB)
    con.executescript("""
        CREATE TABLE businesses (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            maps_url         TEXT UNIQUE NOT NULL,
            nome             TEXT DEFAULT '',
            categoria        TEXT DEFAULT '',
            avaliacao        TEXT DEFAULT '',
            total_avaliacoes TEXT DEFAULT '',
            telefone         TEXT DEFAULT '',
            website          TEXT DEFAULT '',
            instagram        TEXT DEFAULT '',
            endereco         TEXT DEFAULT '',
            posicao          INTEGER,
            scraped_at       TEXT DEFAULT (datetime('now','localtime')),
            updated_at       TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    for i, (nome, cat, nota, total, tel, site, ig, end, pos, _tag) in enumerate(CLINICAS, 1):
        maps_url = f"https://maps.google.com/?cid=SEED{i:03d}"
        con.execute("""
            INSERT INTO businesses
                (maps_url, nome, categoria, avaliacao, total_avaliacoes,
                 telefone, website, instagram, endereco, posicao)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (maps_url, nome, cat, nota, total, tel, site, ig, end, pos))
    con.commit()
    con.close()
    print(f"✅ Seed de origem criado: {SEED_DB.name} ({len(CLINICAS)} clínicas)")


def seed(fazer_ingest: bool = True):
    _criar_source()
    if fazer_ingest:
        ingest_mod.ingest(SEED_DB)


if __name__ == "__main__":
    seed()
    print("Funil após seed:", __import__("db").funnel_counts())
