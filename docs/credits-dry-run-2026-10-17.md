# Credits ladder: dry run (2026-10-17)

What the ladder in `docs/credits-sourcing-design-2026-10-17.md` would do on the live library, before
it's switched on. Nothing was written. Every issue the ladder would pick was asked of both GCD and
Comic Vine, so they can be compared. "Would use" is the ladder's own decision (`record.fill_credits`,
dry run).

**This is the rerun after the implementation review** (`docs/credits-ladder-impl-review-2026-10-17.md`),
on commit `5df6fe6`, live. The first run was on `4214715`; its table is in git history.

## Summary

- The ladder picks **34** issues. All have no credits and a GCD or Comic Vine id. No issue whose
  credits came from LOCG or a file's own tags has either id, so none of those are revisited. The
  other ~330 issues without credits are the 237 unmatched files and issues not yet out, which have no
  id to ask by.
- **GCD has credits for 19**, **Comic Vine for 22**. The ladder gives credits to
  **24 of 34**: 17 from GCD, 7 from Comic Vine. 10 stay empty.
- **Changed since the first run:** Deadpool Corps: Rank and Foul #1 and Hit-Monkey #1. GCD has only
  their cover artists; Comic Vine has the writers and artists, so Comic Vine's list is used (the first
  run used GCD's). Every other issue is as before.
- **Picked once.** After a fill, an issue is picked again only when a catalogue that could do better
  hasn't been asked this week (review, finding 1).
- **Still empty:** 100 Bullets: The US of Anger #4 and The Seasons #12 (out 14 Oct; only LOCG has
  them), and older issues GCD itself hasn't credited and Comic Vine can't be asked about.
- **Metron lists with no writer and no artist: 26.** The ladder doesn't touch Metron's lists, so these
  stay as they are. A separate decision if wanted.
- **Incomplete lists** (measured in the first run): Metron records with no artist went from 435 to
  **44** after the Illustrator fix; with no writer, 56.

## The rule

One catalogue's list, never a blend. A list that names a writer or an artist beats one that doesn't;
between two that both do (or both don't), the higher rung wins: Metron > GCD > Comic Vine > LOCG >
file tags. An empty answer never clears a list. The same comparison applies when Metron rewrites an
issue, so a Metron list with only a cover artist doesn't replace a list with the team.

## Every issue

| Issue | Out | Would use | GCD | Comic Vine |
|---|---|---|---|---|
| 100 Bullets: The US of Anger #4 | 2026-10-14 | stays empty | none | — (no id) |
| The Seasons #12 | 2026-10-14 | stays empty | none | none |
| Batman: Bad Seeds – Gotham Central #2 | 2026-10-14 | GCD | Christopher Cantwell (Writer); Jacob Phillips (Penciller, Inker, Colorist, Cover); Becca Carey (Letterer) | — (no id) |
| Monstress #64 | 2026-09-16 | Comic Vine | none | Jackie Marzan (Letterer); Marjorie Liu (Writer); Sana Takeda (Artist, Colorist, Cover) |
| Marvel Zombies Red Band #5 | 2026-01-28 | GCD | Ethan S. Parker (Writer); Griffin Sheridan (Writer); Nick Lowe (Writer); Janeth Bazaldúa Nava (Penciller, Inker); Erick Arciniega (Colorist); Travis Lanham of Virtual Calligraphy (Letterer) … +2 more | Erick Arciniega (Colorist); Ethan S. Parker (Writer); Greg Land (Cover); Griffin Sheridan (Writer); Jan Bazaldua (Artist); Rachelle Rosenberg (Cover) … +1 more |
| Marvel Zombies Red Band #4 | 2025-12-31 | GCD | Ethan S. Parker (Writer); Griffin Sheridan (Writer); Nick Lowe (Writer); Janeth Bazaldúa Nava (Penciller, Inker); Greg Land (Penciller, Inker, Cover); Erick Arciniega (Colorist) … +2 more | Erick Arciniega (Colorist); Ethan S. Parker (Writer); Greg Land (Cover); Griffin Sheridan (Writer); Jan Bazaldua (Artist); Mike Hawthorne (Cover) … +4 more |
| Marvel Zombies Red Band #3 | 2025-11-26 | GCD | Ethan S. Parker (Writer); Griffin Sheridan (Writer); Nick Lowe (Writer); Janeth Bazaldúa Nava (Penciller, Inker); Greg Land (Penciller, Inker, Cover); Erick Arciniega (Colorist) … +2 more | Erick Arciniega (Colorist); Ethan S. Parker (Writer); Greg Land (Cover); Griffin Sheridan (Writer); Jan Bazaldua (Artist); Rachelle Rosenberg (Cover) … +1 more |
| Marvel Zombies Red Band #2 | 2025-10-22 | GCD | Ethan S. Parker (Writer); Griffin Sheridan (Writer); Nick Lowe (Writer); Janeth Bazaldúa Nava (Penciller, Inker); Greg Land (Penciller, Inker, Cover); Erick Arciniega (Colorist) … +2 more | Adam Warren (Cover); Erick Arciniega (Colorist); Ethan S. Parker (Writer); Greg Land (Cover); Griffin Sheridan (Writer); Jan Bazaldua (Artist) … +7 more |
| Marvel Zombies Red Band #1 | 2025-09-17 | GCD | Ethan S. Parker (Writer); Griffin Sheridan (Writer); Nick Lowe (Writer); Janeth Bazaldúa Nava (Penciller, Inker); Greg Land (Penciller, Inker, Cover); Erick Arciniega (Colorist) … +2 more | Amelia Vidal (Cover); Clayton Crain (Cover); Erick Arciniega (Colorist); Ethan S. Parker (Writer); Greg Land (Cover); Griffin Sheridan (Writer) … +9 more |
| Port of Earth #10 | 2019-05-15 | stays empty | none | — (no id) |
| Hellboy #3 | 2004-02-04 | stays empty | none | — (no id) |
| Hellboy #4 | 2004-02-04 | stays empty | none | — (no id) |
| PopBot #2 | 2002-05-27 | stays empty | none | — (no id) |
| SQUEE! #4 | 1998-05-01 | stays empty | none | — (no id) |
| SQUEE! #3 | 1997-11-01 | stays empty | none | — (no id) |
| SQUEE! #2 | 1997-09-01 | stays empty | none | — (no id) |
| SQUEE! #1 | 1997-04-01 | stays empty | none | — (no id) |
| Deadpool Corps- Rank and Foul #1 | — | Comic Vine | Humberto Ramos (Cover); Edgar Delgado (Cover) | Aaron Mcclellan (Colorist); Chris Biggs (Writer); Damion Scott (Inker); David Wiltfong (Writer); Edgar Delgado (Cover); Eric J. Moreels (Writer) … +19 more |
| Deadpool Pulp #1 | — | GCD | Adam Glass (Writer); Mike Benson (Writer); Laurence Campbell (Penciller, Inker); Jae Lee (Penciller, Inker, Cover); Lee Loughridge (Colorist); June Chung (Colorist, Cover) … +1 more | Adam Glass (Writer); Clayton Cowles (Letterer); Jae Lee (Cover); June Chung (Cover); Laurence Campbell (Artist); Lee Loughridge (Colorist) … +1 more |
| Deadpool Pulp #2 | — | GCD | Adam Glass (Writer); Mike Benson (Writer); Laurence Campbell (Penciller, Inker); Jae Lee (Penciller, Inker, Cover); Lee Loughridge (Colorist); June Chung (Colorist, Cover) … +1 more | Adam Glass (Writer); Clayton Cowles (Letterer); Jae Lee (Cover); June Chung (Cover); Laurence Campbell (Artist); Lee Loughridge (Colorist) … +1 more |
| Deadpool Pulp #3 | — | GCD | Adam Glass (Writer); Mike Benson (Writer); Laurence Campbell (Penciller, Inker); Jae Lee (Penciller, Inker, Cover); Lee Loughridge (Colorist); June Chung (Colorist, Cover) … +1 more | Adam Glass (Writer); Clayton Cowles (Letterer); Jae Lee (Cover); June Chung (Cover); Laurence Campbell (Artist); Lee Loughridge (Colorist) … +1 more |
| Deadpool Pulp #4 | — | GCD | Adam Glass (Writer); Mike Benson (Writer); Laurence Campbell (Penciller, Inker); Lee Loughridge (Colorist); Clayton Cowles of Virtual Calligraphy (Letterer); Jae Lee (Cover) … +1 more | Adam Glass (Writer); Clayton Cowles (Letterer); Jae Lee (Cover); June Chung (Cover); Laurence Campbell (Artist); Lee Loughridge (Colorist) … +1 more |
| Fear Itself Sketchbook #1 | — | Comic Vine | none | Billy Tan (Penciller); Ed Brubaker (Writer); Giuseppe Camuncoli (Penciller); Hunter Gorinson (Writer); James Viscardi (Writer); Laura Martin (Colorist) … +8 more |
| Fear Itself Spotlight #1 | — | GCD | Mike Conroy (Writer); Giuseppe Camuncoli (Penciller, Inker); Mike Norton (Penciller, Inker); Stefano Caselli (Penciller, Inker); Stuart Immonen (Penciller, Inker, Cover) | Chris Arrant (Writer); Dana Perkins (Writer); Dugan Trodglen (Writer); Jess Harrold (Writer); John Rhett Thomas (Writer); Mike Conroy (Writer) … +3 more |
| Hit-Monkey #1 | — | Comic Vine | Dave Johnson (Cover) | Dalibor Talajić (Artist, Colorist); Daniel Way (Writer); Dave Johnson (Cover); Joe Sabino (Letterer) |
| Hit-Monkey #2 | — | Comic Vine | none | Dalibor Talajić (Artist); Daniel Way (Writer); Dave Johnson (Cover); Joe Sabino (Letterer); José Villarrubia (Colorist) |
| Hit-Monkey #3 | — | Comic Vine | none | Dalibor Talajić (Artist); Daniel Way (Writer); Joe Sabino (Letterer); José Villarrubia (Colorist) |
| Marvel Zombies- Dead Days #1 | — | GCD | Robert Kirkman (Writer); Sean Phillips (Penciller, Inker); June Chung (Colorist); Rus Wooton of Virtual Calligraphy (Letterer); Arthur Suydam (Cover) | Arthur Suydam (Artist, Cover); June Chung (Colorist); Robert Kirkman (Writer); Rus Wooton (Letterer); Sean Phillips (Artist) |
| Marvel Zombies- Evil Evolution #1 | — | Comic Vine | none | Adam DeKraker (Penciller); Andres Mossa (Colorist); Andrew Dalhouse (Colorist); Antonio Fabela (Colorist); Carlo Soriano (Artist); Chris Sotomayor (Colorist) … +6 more |
| Spider-Gwen Annual #1 | — | GCD | Jason Latour (Writer, Penciller, Inker, Colorist); Chris Brunner (Penciller, Inker); Emi Lenox (Penciller, Inker); Chris Visions (Penciller, Inker); Olivia Margraf (Penciller, Inker); Annapaola Martello (Penciller, Inker) … +12 more | Alvaro Lopez (Inker); Annapaola Martello (Artist); Chris Brunner (Artist); Chris Visions (Artist); Clayton Cowles (Letterer); James Harren (Artist) … +12 more |
| Judge Dredd - The Complete Case Files #48 | 2025-11-05 | GCD | Al Ewing (Writer); John Wagner (Writer); John Smith (Writer); Gordon Rennie (Writer); Robbie Morrison (Writer); Karl Richardson (Penciller, Inker, Colorist) … +21 more | — (no id) |
| Sonic Super Special #3 | 1997-10-29 | GCD | Michael Gallagher (Writer); Mike Kanterovich (Writer); Ken Penders (Writer); Scott Shaw (Penciller); Dave Manak (Penciller); Jorge Pacheco (Inker) … +11 more | Barry Grossman (Ben Sean) (Colorist); Bill Yoshida (Letterer); Dan Nakrosis (Letterer); Dave Manak (Penciller); Harvey Mercadoocasio (Cover); Henry Scarpelli (Inker) … +9 more |
| Sonic Super Special #1 | 1997-05-07 | GCD | Scott Fulop (Writer); Ken Penders (Writer); Tom Rolston (Writer); Sam Maxwell (Penciller); Dave Manak (Penciller); Jim Amash (Inker) … +7 more | Barry Grossman (Ben Sean) (Colorist); Dave Manak (Penciller); Jeff Powell (Letterer); Jim Amash (Inker); Karl Bollers (Colorist); Ken Penders (Writer) … +7 more |
| Dark Horse Presents #63 | — | GCD | Moebius (Writer, Penciller, Inker, Cover); Randy Stradley (Writer); John Arcudi (Writer); Doug Wheeler (Writer); Dale Eaglesham (Penciller, Inker, Cover); Mike Hoffman (Penciller, Inker, Letterer, Cover) … +5 more | Dale Eaglesham (Artist); Doug Wheeler (Writer); John Arcudi (Writer); Karen Casey-Smith (Letterer); Mike Hoffman (Artist, Letterer); Mœbius (Writer, Artist, Cover) |

## Things to look at

- **Long lists.** Collected editions and anthologies bring every story's team: Judge Dredd: The
  Complete Case Files #48 has 52 credit lines from GCD; Spider-Gwen Annual #1 has 30. The review's
  decision 2: keep the whole list, shorten it on the issue card. The shortening isn't built.
- **Comic Vine's issue-level lists include cover and variant artists.** Deadpool Corps: Rank and
  Foul #1 now shows Comic Vine's 25 names.
- **Still open from the review:** Comic Vine has no hourly guard of its own (finding 3; the ladder's
  share is at most 36 calls an hour); the query also matches `"gcd_id": null` (finding 4, harmless);
  two Comic Vine role spellings aren't mapped (finding 5).

Switch: config `credits_ladder` = `on` (absent = off).
