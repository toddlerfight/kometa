# Related, phase 2: review of the partner's build (2026-10-17)

The design partner built phase 2 (themes from descriptions, a community channel, an
anthology guard) as a separate module, `kometa/related_phase2.py`, with a report
dated 2026-10-16. This is the review against the live database, and what was folded
into `kometa/related_combined.py` instead.

All numbers below come from the harness (`python3 -m kometa.related_eval`) run inside
the container on a copy of the live database, 2026-10-17. The full report is
`phase2-switches-2026-10-17.md` in this folder.

## What held up

- **Themes.** 27 word patterns over the series and issue descriptions tag 123 of 840
  series (zombies 28, vampires 13, occult 13, space opera 11, martial arts 10, ...).
  They add 15 series to coverage and take 11 rows out of "crowded".
- **The anthology problem.** The Joker 80th Anniversary Special (one issue, 25
  writers and artists) was in 23 top-five rows. It sat fifth in Saga's row because
  Fiona Staples drew a pin-up in it.
- **The community signal exists.** CBR's reviewers, centred per person, link
  Something Is Killing the Children to Die and The Silver Coin, and Monstress to
  Saga, Paper Girls and The Wicked + the Divine.

## What did not

1. **The partner's engines shared one index and wrote into it.** The themes engine
   added its features to the combiner's cached index, so every engine run after it
   inherited them. In the partner's report, "community" and "themes + community"
   print identical numbers. Community was never measured on its own.
2. **Community cards measure era, not taste.** Saga's strongest neighbours are
   Hawkeye (2012) and Batman (2011). Superior Spider-Man's is Avengers. Monstress
   gets Spider-Man/Deadpool, scoring like Seven to Eternity. Strength does not
   separate the good pairs from the era pairs.
3. **τ 0.12 flattens support.** Five shared readers scored 1.4 and a hundred scored
   2.0 on a cap of 2.0. Over half of all pairs (6,380 of 11,622) cleared the card bar,
   and 5,292 would have been new community-only cards.
4. **The reason line said nothing.** "Liked by the same readers" was on three of
   Monstress's first four cards.
5. **The proposed anthology rule was too broad.** Counting every credit (editors,
   variant covers) it caught 39 runs, including ordinary one-shots such as Dark
   Days: The Forge, and it removed their creators in both directions.
6. **Theme words that are names.** Watchmen came out "ghosts" (the Silk Spectre),
   Checkmate "monsters" (Leviathan), Knight Terrors: Action Comics "cyberpunk"
   (Cyborg).

## What was built

Inside the combiner, behind two switches in config:

| Switch | Values | Live |
|---|---|---|
| `related_themes` | `on` / off | on |
| `related_community` | `boost` / `cards` / off | boost |

- **Themes** join the genre channel at trust 0.5, below a file's theme (0.6) and
  GCD's (0.8). Keys the tag vocabulary already has use its name (occult,
  post-apocalyptic, zombies, mythology, martial arts, conspiracy). A name guard skips
  spectre, cyborg, android, witch and leviathan when capitalised mid-sentence.
- **Community** keeps the partner's maths: adjusted cosine, at least 5 shared raters
  and 3 co-likers, shrinkage n/(n+25). τ is 0.4. In `boost` it adds to a card another
  channel earned and never makes one. Its reason shows only when it gave a quarter of
  the card's score. `cards` lets it make a card at 1.0 or more. It is built and
  tested, and off.
- **Anthology guard, always on and one-way.** A run of four issues or fewer with more
  than 8 writers and artists per issue is an anthology. That rule catches 15 runs. A
  contributor in only one of its issues is no longer a way into it from other rows.
  The anthology's own row still walks all of them.
- **Hub gate count.** The engine counted story arcs when it set the 3% limit and the
  harness did not, which let a 25th row through a gate of 24.
- **Speed.** A series' themes are kept against its blurbs, so a rebuild rereads only
  what changed. A warm rebuild with both switches on takes 0.85 s, the same as phase
  1. The first build after a restart pays 1.5 s more.

## Gates

| Gate | Phase 1 | + themes | + themes + boost | + themes + cards |
|---|---|---|---|---|
| Series with 4+ cards (target 700) | 675 | 690 | 690 | 691 |
| Crowded rows | 123 | 112 | 112 | 109 |
| Largest hub (max 3%) | 2.8% | 2.9% | 2.9% | 2.9% |
| Weak-credit cards | 0 | 0 | 0 | 0 |
| Speed, p95 | 0.4 ms | 0.4 ms | 0.4 ms | 0.4 ms |

Community cards at these settings add one series. The partner's 709 to 711 came
from the weak pairs and from the anthology acting as many rows' fourth card.

## Rows that changed (top six)

- **Saga.** Paper Girls, We Stand On Guard, Ex Machina, then Black Science, Star
  Wars: Vader Dark Visions and Farmhand. These replace 100%, Destination Kill and
  Dune. The Joker anthology is gone.
- **Monstress.** Paper Girls, The Sacrificers, Seven to Eternity, I Hate Fairyland,
  The Seasons, What's the Furthest Place From Here?
- **Something Is Killing the Children.** Department of Truth, The Nice House by the
  Sea, w0rldtr33, then Die, Plush and Stray Dogs.
- **Invincible, Fantastic Four, Aliens: Berserker** are unchanged.

## Open

- Whether `cards` is ever worth switching on. That needs the owner's blind scoring of
  community-only cards, and at these settings it adds almost nothing.
- Coverage is 10 short of 700. Of the 75 series with no cards (themes + boost), 53
  have no credits, tags, descriptions or reviews at all yet. The Metron refill and
  GCD genres are still landing.
