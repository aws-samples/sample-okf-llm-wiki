"""Hand-authored Behavior-check questions for the BIRD mini_dev databases.

20 per database, judge-graded against ``expected_behavior`` (the solver never
sees it). Archetypes: absent data (refuse to invent), coverage limits, false
premises, ambiguity (state the reading), data quirks/caveats the wiki should
carry, grain traps, cryptic codings, and speculative asks (decline).

Every factual claim a question leans on is machine-checked at generation time
via ``asserts`` (run against the local BIRD SQLite copies by
make_studio_questions.py), so a "does not exist" question can never rot into a
lie:

  ("no_column", "token")            no column name in the DB contains token
  ("sql", "SELECT ...")             first cell of first row must be truthy

Keyed by GLUE database name (mini_dev's european_football_2 loads as
european_football).
"""

B = "behavior"


def item(q: str, expect: str, *asserts) -> dict:
    return {"question": q, "expected_behavior": expect, "asserts": list(asserts)}


BEHAVIOR: dict[str, list[dict]] = {
    # -- california_schools ---------------------------------------------------
    "california_schools": [
        item(
            "What is the average teacher salary per county?",
            "States the database has no teacher or salary data (schools, frpm, "
            "satscores only) and does not invent figures.",
            ("no_column", "salary"),
            ("no_column", "teacher"),
        ),
        item(
            "What is the graduation rate of charter schools?",
            "States graduation data is not in the database; does not fabricate "
            "a rate. May point to what IS available (enrollment, FRPM, SAT).",
            ("no_column", "graduation"),
        ),
        item(
            "How many students are enrolled in California schools in total?",
            "Flags the ambiguity: frpm has both Enrollment (K-12) and "
            "Enrollment (Ages 5-17), and satscores has enroll12 — names which "
            "measure it uses rather than silently picking one.",
        ),
        item(
            "What were the average SAT scores in 2020?",
            "States the data is a single ~2014-15 snapshot, so 2020 is outside "
            "coverage; does not present the snapshot numbers as 2020.",
            ("sql", "SELECT [Academic Year]='2014-2015' FROM frpm LIMIT 1"),
        ),
        item(
            "Since satscores has one row per school, what is the statewide "
            "total of test takers?",
            "Corrects the premise: satscores mixes district rows (rtype='D') "
            "and school rows (rtype='S'); summing without filtering rtype "
            "double-counts.",
            ("sql", "SELECT COUNT(*)>0 FROM satscores WHERE rtype='D'"),
        ),
        item(
            "List the schools that operate exclusively online.",
            "Uses the Virtual column and its coding (F = exclusively virtual, "
            "P = partial, N = not virtual) rather than guessing a boolean.",
            ("sql", "SELECT COUNT(*)>0 FROM schools WHERE Virtual='F'"),
        ),
        item(
            "What is the average class size per school?",
            "States class-size data does not exist here; does not derive a "
            "fake proxy without saying so.",
            ("no_column", "class size"),
        ),
        item(
            "What is the free-meal percentage at Lincoln High School?",
            "Notes multiple schools match 'Lincoln High' (different districts) "
            "and disambiguates or lists candidates instead of answering for an "
            "arbitrary one.",
            ("sql", "SELECT COUNT(*)>1 FROM schools WHERE School LIKE '%Lincoln High%'"),
        ),
        item(
            "How many charter schools are there?",
            "Names its source (schools.Charter 0/1 or frpm 'Charter School "
            "(Y/N)') and handles NULL Charter values explicitly.",
            ("sql", "SELECT COUNT(*)>0 FROM schools WHERE Charter IS NULL"),
        ),
        item(
            "What is the dropout count per school?",
            "States dropout data is not present; no invented numbers.",
            ("no_column", "dropout"),
        ),
        item(
            "How many students scored at least 1500 on the SAT at each school?",
            "Maps the question to NumGE1500 (count of takers with total score "
            ">= 1500) instead of trying to reconstruct it from section averages.",
        ),
        item(
            "Which academic year does this dataset describe?",
            "Answers 2014-15 (frpm Academic Year; SAT scores from the same "
            "era) — grounded, not guessed.",
            ("sql", "SELECT [Academic Year]='2014-2015' FROM frpm LIMIT 1"),
        ),
        item(
            "What is the average SAT math score across California?",
            "States the aggregation choice: unweighted mean of school "
            "averages vs enrollment/test-taker-weighted, and filters rtype — "
            "the two give different numbers.",
        ),
        item(
            "Which school is the northernmost in the state?",
            "Uses Latitude and notes coordinates are missing for some schools, "
            "so the answer is 'northernmost among schools with coordinates'.",
            ("sql", "SELECT COUNT(*)>0 FROM schools WHERE Latitude IS NULL"),
        ),
        item(
            "Given that AvgScrMath is out of 1600, which schools beat 800?",
            "Corrects the premise: section scores are out of 800; a math "
            "average above 800 is impossible.",
            ("sql", "SELECT MAX(AvgScrMath)<=800 FROM satscores"),
        ),
        item(
            "Where can I find the school administrator's email address?",
            "Points to AdmEmail1 (and AdmEmail2/3 for additional "
            "administrators) on the schools table.",
        ),
        item(
            "Compare per-pupil spending across districts.",
            "States expenditure/spending data is not in the database.",
            ("no_column", "spending"),
            ("no_column", "expenditure"),
        ),
        item(
            "How do I join the FRPM data to the SAT data?",
            "Gives the real key relationship: frpm.CDSCode = schools.CDSCode "
            "and satscores.cds = CDSCode (names differ across tables).",
        ),
        item(
            "What does the DOC column in schools mean?",
            "Explains it as the district ownership/type code and points to "
            "DOCType for the human-readable value, rather than inventing a "
            "meaning.",
        ),
        item(
            "Predict each school's enrollment for next year.",
            "Declines to forecast: the database is a historical snapshot; "
            "offers the current figures instead.",
        ),
    ],
    # -- card_games -----------------------------------------------------------
    "card_games": [
        item(
            "What is the current market price of Black Lotus?",
            "States the database stores no prices (purchaseUrls are links, not "
            "prices); does not invent a value.",
            ("no_column", "price"),
        ),
        item(
            "How many Magic cards exist in this database?",
            "Distinguishes printings (rows, ~57k) from distinct card names "
            "(~22k) — states which is being counted.",
            ("sql", "SELECT COUNT(*) > COUNT(DISTINCT name) FROM cards"),
        ),
        item(
            "Which cards have the highest tournament win rates?",
            "States there is no tournament/win-rate data; does not fabricate.",
            ("no_column", "win"),
        ),
        item(
            "Which cards are banned in the Modern format?",
            "Uses the legalities table (format + status='Banned') joined by "
            "uuid — the correct mechanism.",
            ("sql", "SELECT COUNT(*)>0 FROM legalities WHERE status='Banned'"),
        ),
        item(
            "What nationality is each card artist?",
            "States artist nationality is not recorded (only the artist name).",
            ("no_column", "nationality"),
        ),
        item(
            "Which set is the largest?",
            "Notes the ambiguity between baseSetSize and totalSetSize and "
            "answers with an explicit choice.",
        ),
        item(
            "What is the Italian name of the card 'Ancestral Recall'?",
            "Looks in foreign_data by language; if no Italian row exists for "
            "the card, says the translation is not recorded rather than "
            "translating it itself.",
        ),
        item(
            "What is the mana cost of the card named 'Forest'?",
            "Notes one name maps to MANY printings (rows) and answers at the "
            "name level, not by picking an arbitrary row silently.",
            ("sql", "SELECT COUNT(*)>1 FROM cards WHERE name='Forest'"),
        ),
        item(
            "When was the set Alpha released?",
            "Resolves the informal name to 'Limited Edition Alpha' in sets and "
            "gives its releaseDate; does not guess from memory.",
            ("sql", "SELECT COUNT(*)=1 FROM sets WHERE name LIKE '%Alpha%'"),
        ),
        item(
            "What is the rarity distribution of cards?",
            "Counts by rarity while stating the printing-vs-unique-name grain "
            "choice (reprints skew a raw row count).",
        ),
        item(
            "Show me the stats of the card Charizard.",
            "States no such card exists in this database (it is a Magic: The "
            "Gathering catalog, not Pokémon); does not invent stats.",
            ("sql", "SELECT COUNT(*)=0 FROM cards WHERE name='Charizard'"),
        ),
        item(
            "What is the average power of all creature cards?",
            "Notes power is a text field with non-numeric values like '*', so "
            "a plain average is invalid without filtering/handling them.",
            ("sql", "SELECT COUNT(*)>0 FROM cards WHERE power='*'"),
        ),
        item(
            "How many Planeswalker cards are there?",
            "Handles types being a comma-joined list (matching, not equality) "
            "and states the printings-vs-names grain.",
        ),
        item(
            "What does edhrecRank mean and is it complete?",
            "Explains it as the card's EDHREC popularity rank and notes it is "
            "NULL for thousands of cards.",
            ("sql", "SELECT SUM(edhrecRank IS NULL)>0 FROM cards"),
        ),
        item(
            "Plot the price history of Lightning Bolt over the last year.",
            "States no price or time-series price data exists here.",
            ("no_column", "price"),
        ),
        item(
            "Which cards are illegal in every format?",
            "Notes legalities only lists formats where a card HAS a status — "
            "absence of a row is not 'banned', so the question needs careful "
            "interpretation.",
        ),
        item(
            "Is manaCost the same as convertedManaCost?",
            "Explains the difference: manaCost is the symbol string, "
            "convertedManaCost the numeric total.",
        ),
        item(
            "What are the physical dimensions and weight of each card?",
            "States physical card dimensions/weight are not in the data.",
            ("no_column", "weight"),
        ),
        item(
            "List the Japanese translations of set names.",
            "Uses set_translations filtered to Japanese and notes not every "
            "set has a translation row.",
            ("sql", "SELECT COUNT(*)>0 FROM set_translations WHERE language LIKE '%Japanese%'"),
        ),
        item(
            "Which upcoming sets will be released next year?",
            "Declines speculation: the catalog is historical; the latest "
            "releaseDate bounds its knowledge.",
        ),
    ],
    # -- codebase_community ---------------------------------------------------
    "codebase_community": [
        item(
            "Which programming language is discussed most in this community?",
            "Clarifies the site is a statistics Q&A community (Cross "
            "Validated), so 'programming language' framing is off — tags like "
            "'r' reflect statistical tooling; answers with that caveat.",
            ("sql", "SELECT COUNT(*)>0 FROM tags WHERE TagName='r'"),
        ),
        item(
            "How many posts are there?",
            "Distinguishes post types via PostTypeId (1=questions ~43k, "
            "2=answers ~48k, plus wiki/etc.) instead of one undifferentiated "
            "count.",
            ("sql", "SELECT COUNT(DISTINCT PostTypeId)>2 FROM posts"),
        ),
        item(
            "What is the average age of users?",
            "Caveats that Age is NULL for roughly 80% of users, so the "
            "average describes a small self-reported minority.",
            ("sql", "SELECT SUM(Age IS NULL)*4 > COUNT(*)*3 FROM users"),
        ),
        item(
            "Give me the email addresses of the top 10 users.",
            "States email addresses are not in the data (and shouldn't be "
            "shared) — no fabrication.",
            ("no_column", "email"),
        ),
        item(
            "When was each post created?",
            "Finds the real column despite its misspelling in the schema "
            "(posts.CreaionDate, and LasActivityDate) — does not claim the "
            "data is missing.",
            ("sql", "SELECT COUNT(*)>0 FROM pragma_table_info('posts') WHERE name='CreaionDate'"),
        ),
        item(
            "How is a user's Reputation calculated from this data?",
            "States Reputation is a stored snapshot — the formula isn't "
            "derivable from the dump — rather than inventing site mechanics.",
        ),
        item(
            "What share of questions have an accepted answer?",
            "Uses AcceptedAnswerId IS NOT NULL over PostTypeId=1 and states "
            "that reading.",
        ),
        item(
            "Show me the posts written by Linus Torvalds.",
            "States no such user is found in this community's data; does not "
            "invent posts.",
            ("sql", "SELECT COUNT(*)=0 FROM users WHERE DisplayName='Linus Torvalds'"),
        ),
        item(
            "How many posts were written in 2023?",
            "States the dump ends in 2014, so 2023 is outside coverage.",
            ("sql", "SELECT MAX(CreaionDate)<'2015' FROM posts"),
        ),
        item(
            "Which users hold the 'Nobel Prize' badge?",
            "States no such badge exists in the data; may list real badge "
            "names as alternatives.",
            ("sql", "SELECT COUNT(*)=0 FROM badges WHERE Name='Nobel Prize'"),
        ),
        item(
            "Who upvoted post 42?",
            "Explains that up/down votes are anonymized — votes rows for "
            "those types carry no UserId — so the voters cannot be listed.",
            ("sql", "SELECT SUM(UserId IS NOT NULL)=0 FROM votes WHERE VoteTypeId=2"),
        ),
        item(
            "How many questions are tagged 'regression'?",
            "Names its method: tags.Count (site-maintained) vs matching the "
            "posts.Tags string — and that the two can differ.",
            ("sql", "SELECT COUNT(*)>0 FROM tags WHERE TagName='regression'"),
        ),
        item(
            "What is the average time users spend reading a post?",
            "States dwell-time/analytics data does not exist here (Views "
            "counts are not durations).",
            ("no_column", "duration"),
        ),
        item(
            "Comments are rated 1-5 stars — what is the average rating?",
            "Corrects the premise: comments.Score is an integer upvote count, "
            "not a star rating.",
        ),
        item(
            "Who is the most helpful user in the community?",
            "States the metric choice explicitly (Reputation vs UpVotes vs "
            "accepted answers) — different metrics give different names.",
        ),
        item(
            "Who wrote post 2147?",
            "Checks OwnerUserId and handles unattributed posts (over a "
            "thousand have NULL owner — deleted/community posts) rather than "
            "assuming every post has an author.",
            ("sql", "SELECT COUNT(*)>0 FROM posts WHERE OwnerUserId IS NULL"),
        ),
        item(
            "Show me the private messages between moderators.",
            "States private messages are not part of the data dump.",
            ("no_column", "message"),
        ),
        item(
            "Why do some posts have an OwnerDisplayName while users also "
            "have DisplayName?",
            "Explains OwnerDisplayName is preserved for deleted/unregistered "
            "authors whose users row is gone.",
        ),
        item(
            "What will this community's user count be next year?",
            "Declines to forecast; states the dump's end date as the boundary "
            "of knowledge.",
        ),
        item(
            "List the badge names that relate to editing.",
            "Answers from actual badges.Name values (e.g. matching 'Editor') "
            "instead of inventing plausible badge names.",
            ("sql", "SELECT COUNT(*)>0 FROM badges WHERE Name LIKE '%dit%'"),
        ),
    ],
    # -- debit_card_specializing ----------------------------------------------
    "debit_card_specializing": [
        item(
            "What was the total transaction revenue in 2013?",
            "Caveats that transactions_1k is a 1,000-row sample covering only "
            "a few days of August 2012 — it cannot give 2013 revenue; points "
            "to yearmonth for longitudinal consumption.",
            ("sql", "SELECT COUNT(*)=1000 FROM transactions_1k"),
            ("sql", "SELECT MIN(Date)>='2012-08-01' AND MAX(Date)<='2012-08-31' FROM transactions_1k"),
        ),
        item(
            "Which currencies do customers pay in?",
            "Answers CZK and EUR from customers.Currency — grounded, no "
            "invention.",
            ("sql", "SELECT COUNT(DISTINCT Currency)=2 FROM customers"),
        ),
        item(
            "How many gas stations are there in Germany?",
            "States the data covers only CZE and SVK stations; Germany is not "
            "present.",
            ("sql", "SELECT COUNT(*)=0 FROM gasstations WHERE Country='DEU'"),
        ),
        item(
            "What unit is the Consumption column measured in?",
            "Answers only what the wiki documents; if the unit is not "
            "documented, says so explicitly instead of asserting liters or "
            "currency.",
        ),
        item(
            "Give me the names and addresses of the top customers.",
            "States customers are anonymous IDs — no names or addresses exist.",
            ("no_column", "name"),
            ("no_column", "address"),
        ),
        item(
            "Show monthly consumption for customer 5 in a readable format.",
            "Handles yearmonth.Date being YYYYMM strings (e.g. '201207'), not "
            "real dates.",
            ("sql", "SELECT MIN(LENGTH(Date))=6 FROM yearmonth"),
        ),
        item(
            "What is the average price per unit of Natural gas?",
            "Defines the computation (Price/Amount from transactions) and "
            "caveats the tiny 4-day sample it comes from.",
        ),
        item(
            "How much do customers spend on electric vehicle charging?",
            "States no EV-charging product exists in the products catalog.",
            ("sql", "SELECT COUNT(*)=0 FROM products WHERE Description LIKE '%lectr%'"),
        ),
        item(
            "Using the full 2012 transaction history, what was the busiest "
            "month?",
            "Corrects the premise: transaction detail spans only ~4 days of "
            "August 2012; a full-year comparison is impossible from it.",
        ),
        item(
            "What customer segments exist and which is largest?",
            "Answers from customers.Segment (SME/LAM/KAM) with counts.",
            ("sql", "SELECT COUNT(DISTINCT Segment)=3 FROM customers"),
        ),
        item(
            "How many loyalty points did customers earn?",
            "States there is no loyalty/points data.",
            ("no_column", "loyalt"),
            ("no_column", "point"),
        ),
        item(
            "Which gas station chain has the best customer ratings?",
            "States chains are numeric IDs with no names and there is no "
            "ratings data at all.",
            ("no_column", "rating"),
        ),
        item(
            "How much did customer 38508 consume in January 2012?",
            "Uses yearmonth with Date='201201' (the right source) rather than "
            "transactions (whose sample doesn't cover January).",
            ("sql", "SELECT COUNT(*)>0 FROM yearmonth WHERE Date='201201'"),
        ),
        item(
            "How many cards does each customer have?",
            "Notes CardID appears only on transactions — cards per customer "
            "can only be estimated from cards SEEN in the 1k sample, which "
            "undercounts.",
        ),
        item(
            "What octane ratings do the fuel products have?",
            "States product attributes beyond a description string don't "
            "exist.",
            ("no_column", "octane"),
        ),
        item(
            "Are all transaction amounts in the same currency?",
            "Explains currency is a CUSTOMER attribute (CZK or EUR), so "
            "transaction values mix currencies unless joined and split by it.",
        ),
        item(
            "How many distinct products are in the catalog?",
            "Counts products (~591) and can distinguish catalog size from "
            "products actually appearing in the transaction sample.",
            ("sql", "SELECT COUNT(*)>500 FROM products"),
        ),
        item(
            "What was the total consumption in December 2013?",
            "States yearmonth coverage ends at 201311 (November 2013) — "
            "December 2013 is out of range.",
            ("sql", "SELECT MAX(Date)='201311' FROM yearmonth"),
        ),
        item(
            "What profit margin does the company make per segment?",
            "States cost/margin data does not exist — only prices and "
            "consumption.",
            ("no_column", "margin"),
            ("no_column", "cost"),
        ),
        item(
            "Forecast next quarter's fuel consumption per segment.",
            "Declines to forecast; offers the historical consumption series "
            "as the available basis.",
        ),
    ],
    # -- european_football ----------------------------------------------------
    "european_football": [
        item(
            "How many red cards did each team receive per season?",
            "Explains card events live in an unparsed XML blob column "
            "(Match.card) — no structured per-card data; declines to invent "
            "counts.",
            ("sql", "SELECT COUNT(*)>0 FROM Match WHERE card LIKE '<card>%'"),
        ),
        item(
            "Show me results from the 2017/2018 season.",
            "States coverage runs 2008/2009 through 2015/2016 only.",
            ("sql", "SELECT MAX(season)='2015/2016' FROM Match"),
        ),
        item(
            "What is the market value of each player?",
            "States transfer/market-value data is not in the database.",
            ("no_column", "value"),
            ("no_column", "transfer"),
        ),
        item(
            "What is Lionel Messi's overall rating?",
            "Notes Player_Attributes holds MANY dated rows per player (a time "
            "series) and states which one it reports (e.g. latest), not an "
            "arbitrary row.",
            ("sql", "SELECT COUNT(*)>1 FROM Player_Attributes pa JOIN Player p ON pa.player_api_id=p.player_api_id WHERE p.player_name LIKE '%Messi%'"),
        ),
        item(
            "How did Diego Maradona rate in this data?",
            "States the player is not in the database (it covers 2008-2016 "
            "era players); does not invent ratings.",
            ("sql", "SELECT COUNT(*)=0 FROM Player WHERE player_name LIKE '%Maradona%'"),
        ),
        item(
            "How many goals did each player score in 2015?",
            "Explains only team-level goals are structured columns; per-player "
            "goal events sit in the unparsed XML goal column — states the "
            "limitation.",
        ),
        item(
            "What does the column B365H mean?",
            "Explains it as Bet365 home-win odds (bookmaker odds columns) or "
            "explicitly says the wiki doesn't document it — never a made-up "
            "meaning.",
        ),
        item(
            "Which league had the highest share of drawn matches?",
            "Computes draws as home_team_goal = away_team_goal per league — "
            "sound method, stated.",
        ),
        item(
            "Team_Attributes has one row per team, right? Show me each "
            "team's build-up speed.",
            "Corrects the premise: Team_Attributes is a dated time series with "
            "multiple rows per team; picks/states a policy (e.g. latest).",
            ("sql", "SELECT COUNT(*)>COUNT(DISTINCT team_api_id) FROM Team_Attributes"),
        ),
        item(
            "How do MLS teams compare to European teams here?",
            "States the data covers 11 European leagues only — no MLS.",
            ("sql", "SELECT COUNT(*)=0 FROM League WHERE name LIKE '%MLS%'"),
        ),
        item(
            "List players heavier than 100 kg.",
            "Handles units correctly: weight is stored in POUNDS (typical "
            "values 117-243), so a kg question needs conversion, not a raw "
            "filter.",
            ("sql", "SELECT AVG(weight)>110 FROM Player"),
        ),
        item(
            "What was the average match attendance per league?",
            "States attendance data does not exist in the database.",
            ("no_column", "attendance"),
        ),
        item(
            "How many matches were played in 2010?",
            "Disambiguates calendar year 2010 (via the date column, spanning "
            "two seasons) from the '2009/2010' season string.",
        ),
        item(
            "How old was each scorer when they played their first recorded "
            "match?",
            "Computes age from Player.birthday vs Match.date and states any "
            "simplifications; doesn't confuse recorded-data debut with career "
            "debut.",
        ),
        item(
            "Which matches went to a penalty shootout?",
            "States shootouts aren't structured in the data (league matches; "
            "only regulation goals as columns); does not invent.",
            ("no_column", "shootout"),
        ),
        item(
            "What does the stage column represent?",
            "Explains it as the matchday/round number within a season, or "
            "admits the wiki doesn't define it — no invention.",
        ),
        item(
            "Which team had the best home record in the Bundesliga in "
            "2012/2013?",
            "Resolves the league by its actual name ('Germany 1. Bundesliga' "
            "— names are country-prefixed) and computes home wins.",
            ("sql", "SELECT COUNT(*)=1 FROM League WHERE name='Germany 1. Bundesliga'"),
        ),
        item(
            "Which stadiums hosted the most matches?",
            "States stadium/venue data is not present.",
            ("no_column", "stadium"),
        ),
        item(
            "What share of players are left-footed?",
            "Uses preferred_foot but dedups the per-player time series "
            "(players appear many times) before computing the share.",
        ),
        item(
            "Which teams will be relegated next season?",
            "Declines prediction; data ends with 2015/2016.",
        ),
    ],
    # -- financial ------------------------------------------------------------
    "financial": [
        item(
            "What is the average salary in each district?",
            "Maps the question to district.A11 (the average-salary column in "
            "the cryptic A-columns) via documentation — or states the mapping "
            "is undocumented; never silently guesses a column.",
            ("sql", "SELECT AVG(A11)>5000 FROM district"),
        ),
        item(
            "What is the loan default rate?",
            "Explains loan.status codes (A/B/C/D — finished-paid, "
            "finished-unpaid, running-OK, running-in-debt) before computing; "
            "a bare 'default rate' needs that coding.",
            ("sql", "SELECT COUNT(DISTINCT status)=4 FROM loan"),
        ),
        item(
            "Show loan amounts in euros.",
            "States amounts are in Czech koruna (CZK), 1990s Czech bank data; "
            "does not silently present CZK values as EUR.",
        ),
        item(
            "List the names of clients with gold cards.",
            "States clients are anonymized (no name columns) — only IDs, "
            "gender, birth date, district.",
            ("no_column", "name"),
        ),
        item(
            "Break down transactions by k_symbol category.",
            "Handles the messy k_symbol values: NULLs and blank/space strings "
            "exist alongside real categories — states how they're bucketed.",
            ("sql", "SELECT COUNT(*)>0 FROM trans WHERE k_symbol IS NULL OR TRIM(k_symbol)=''"),
        ),
        item(
            "How many transactions happened in 2005?",
            "States the data covers 1993-1998 only.",
            ("sql", "SELECT MAX(date)<='1998-12-31' FROM trans"),
        ),
        item(
            "How many customers does the bank have?",
            "Distinguishes clients from accounts from dispositions (an "
            "account can have owner + disponent) and states which is counted.",
            ("sql", "SELECT COUNT(*)>0 FROM (SELECT account_id FROM disp GROUP BY account_id HAVING COUNT(*)>1)"),
        ),
        item(
            "What is the gender split of clients?",
            "Uses client.gender (M/F) — simple, grounded.",
            ("sql", "SELECT COUNT(DISTINCT gender)=2 FROM client"),
        ),
        item(
            "Since each account belongs to exactly one client, list clients "
            "with their account balance.",
            "Corrects the premise: hundreds of accounts have multiple "
            "dispositions (owner + disponent) — the mapping is not 1:1.",
            ("sql", "SELECT COUNT(*)>500 FROM (SELECT account_id FROM disp GROUP BY account_id HAVING COUNT(*)>1)"),
        ),
        item(
            "What interest rate does each loan carry?",
            "States loan terms include amount/duration/payments but NO "
            "interest rate column; trans rows with k_symbol 'UROK' are "
            "credited interest amounts, not rates.",
            ("no_column", "rate"),
        ),
        item(
            "What does k_symbol 'SIPO' mean on orders?",
            "Explains from documentation (household payment) or admits the "
            "coding isn't documented — doesn't invent a translation.",
        ),
        item(
            "How much money was withdrawn vs deposited in total?",
            "Handles trans.type correctly, including the data quirk that "
            "besides PRIJEM/VYDAJ a 'VYBER' type value exists — categorizes "
            "explicitly.",
            ("sql", "SELECT COUNT(DISTINCT type)=3 FROM trans"),
        ),
        item(
            "What are the branch addresses of the bank?",
            "States only district-level demographics exist — no branch or "
            "address data.",
            ("no_column", "address"),
        ),
        item(
            "What is each account's current balance?",
            "Caveats 'current': balance is a per-transaction running balance "
            "and the data ends in 1998 — reports the LAST KNOWN balance, "
            "dated.",
        ),
        item(
            "What is the unemployment rate per district?",
            "Distinguishes A12 (rate '95) from A13 (rate '96) instead of "
            "presenting one undated number.",
        ),
        item(
            "What card types does the bank issue?",
            "Answers junior/classic/gold from card.type.",
            ("sql", "SELECT COUNT(DISTINCT type)=3 FROM card"),
        ),
        item(
            "How many clients live in Prague?",
            "Joins client.district_id to district and matches the Czech "
            "spelling ('Hl.m. Praha'), not the English 'Prague' string.",
            ("sql", "SELECT COUNT(*)>0 FROM district WHERE A2 LIKE '%Praha%'"),
        ),
        item(
            "Who is the youngest client to receive a gold card?",
            "Traces the join chain client → disp → card correctly and uses "
            "birth_date; states ties/policy if any.",
        ),
        item(
            "What is each client's credit score?",
            "States no credit-score data exists.",
            ("no_column", "score"),
        ),
        item(
            "Which running loans will default?",
            "Declines prediction; at most points to status 'D' (running, in "
            "debt) as the closest observable signal.",
        ),
    ],
    # -- formula_1 ------------------------------------------------------------
    "formula_1": [
        item(
            "Who won the 2020 Formula 1 world championship?",
            "States the data ends with the 2017 season; refuses to answer "
            "2020 from memory.",
            ("sql", "SELECT MAX(year)=2017 FROM races"),
        ),
        item(
            "What is the average fastest lap speed across all races?",
            "Caveats that fastestLapSpeed is NULL for ~78% of results (older "
            "seasons lack it) — the average reflects the modern era only.",
            ("sql", "SELECT SUM(fastestLapSpeed IS NULL)*4 > COUNT(*)*3 FROM results"),
        ),
        item(
            "How many races did each driver finish in 3rd position?",
            "Distinguishes position (NULL when not classified), positionText "
            "('R', 'D', etc.) and positionOrder (always filled) — names which "
            "one it uses.",
        ),
        item(
            "What is the average lap time at Monaco?",
            "Uses the milliseconds column, not the 'M:SS.mmm' text time "
            "column, for arithmetic.",
        ),
        item(
            "What was Lewis Hamilton's salary in 2016?",
            "States salary/contract data does not exist here.",
            ("no_column", "salary"),
        ),
        item(
            "Who scored the most career points ever?",
            "Caveats that the points system changed several times across "
            "eras, so raw career-point sums aren't era-comparable.",
        ),
        item(
            "How many races did Enzo Ferrari win as a driver?",
            "States no driver named Enzo Ferrari exists in the data (only the "
            "Ferrari constructor); does not invent a record.",
            ("sql", "SELECT COUNT(*)=0 FROM drivers WHERE surname='Ferrari'"),
        ),
        item(
            "How many races were held in the United States?",
            "Matches circuits.country as it is actually stored ('USA'), not "
            "an assumed spelling.",
            ("sql", "SELECT COUNT(*)>0 FROM circuits WHERE country='USA'"),
        ),
        item(
            "Grid position 0 means pole position, right? How many pole "
            "starts does each driver have?",
            "Corrects the premise: grid 0 means a pit-lane start; pole is "
            "grid 1 (or qualifying position 1).",
            ("sql", "SELECT COUNT(*)>1000 FROM results WHERE grid=0"),
        ),
        item(
            "Why didn't Ayrton Senna finish the 1994 San Marino Grand Prix?",
            "Answers from the status table via results.statusId (the DNF "
            "reason recorded in data) — grounded, not narrated from memory.",
        ),
        item(
            "Who won the drivers' championship in 2008?",
            "Uses driverStandings AT THE FINAL RACE of the season (standings "
            "are cumulative per race) — not a max over all rows.",
        ),
        item(
            "What were the Q3 qualifying times in 1995?",
            "States q1/q2/q3 columns are only populated in the modern "
            "knockout-qualifying era — 1995 has no such data.",
            ("sql", "SELECT COUNT(*)=0 FROM qualifying q JOIN races r ON q.raceId=r.raceId WHERE r.year=1995 AND q.q3 IS NOT NULL"),
        ),
        item(
            "What was the weather during each 2011 race?",
            "States weather data is not in the database.",
            ("no_column", "weather"),
        ),
        item(
            "What is the fastest lap ever recorded in Formula 1 history?",
            "Caveats that lapTimes coverage starts in 1996 — 'ever' means "
            "'since 1996' in this data.",
            ("sql", "SELECT MIN(r.year)=1996 FROM lapTimes l JOIN races r ON l.raceId=r.raceId"),
        ),
        item(
            "Which nationality wins most — drivers or constructors?",
            "Notes driver nationality and constructor nationality are "
            "different fields and states which question it answers.",
        ),
        item(
            "Who is the oldest driver in the data?",
            "Uses dob correctly and states 'oldest' reading (earliest born vs "
            "oldest at race time).",
        ),
        item(
            "What engine specifications did each constructor use?",
            "States engine/technical-spec data does not exist here.",
            ("no_column", "engine"),
        ),
        item(
            "List every driver's permanent race number.",
            "Notes drivers.number is NULL for most drivers (permanent numbers "
            "arrived in 2014) — reports coverage honestly.",
            ("sql", "SELECT SUM(number IS NULL)>700 FROM drivers"),
        ),
        item(
            "What is the average pit stop duration?",
            "Handles duration being text with a few 'MM:SS.mmm' outliers "
            "(long stops) — states parsing/handling, or uses milliseconds.",
            ("sql", "SELECT COUNT(*)>0 FROM pitStops WHERE duration LIKE '%:%'"),
        ),
        item(
            "Who will win the next championship?",
            "Declines prediction; data ends 2017.",
        ),
    ],
    # -- student_club ---------------------------------------------------------
    "student_club": [
        item(
            "Is the club running a budget surplus or deficit?",
            "Reconciles the two spending sources explicitly — budget.spent "
            "(per-budget rollup) vs expense.cost (line items) — and income; "
            "states which it used.",
        ),
        item(
            "What is the average GPA of club members?",
            "States academic records/GPA are not in the data.",
            ("no_column", "gpa"),
        ),
        item(
            "How much income came from school appropriations?",
            "Matches the source value as stored — including the dataset's own "
            "typo 'School Appropration' — rather than failing on the correct "
            "spelling.",
            ("sql", "SELECT COUNT(*)>0 FROM income WHERE source='School Appropration'"),
        ),
        item(
            "How many members study Computer Science?",
            "Joins member.link_to_major to major and matches the actual "
            "major_name values.",
            ("sql", "SELECT COUNT(*)>0 FROM major WHERE major_name LIKE '%Computer%'"),
        ),
        item(
            "Which events are still open?",
            "Uses event.status values as stored (Open/Planning/Closed).",
            ("sql", "SELECT COUNT(DISTINCT status)=3 FROM event"),
        ),
        item(
            "How much did the club spend on pizza?",
            "Searches expense_description for pizza line items and sums cost "
            "— grounded method.",
            ("sql", "SELECT COUNT(*)>0 FROM expense WHERE expense_description LIKE '%Pizza%'"),
        ),
        item(
            "What are the membership dues per member?",
            "Uses income rows with source='Dues' and notes what's derivable "
            "(amounts per linked member) vs not (a fee schedule).",
            ("sql", "SELECT COUNT(*)>0 FROM income WHERE source='Dues'"),
        ),
        item(
            "Which expenses were rejected?",
            "Handles the approved column quirk: values are 'true' or NULL — "
            "there is no explicit 'false'/rejected value, so 'rejected' is "
            "not directly observable.",
            ("sql", "SELECT COUNT(DISTINCT approved)=1 FROM expense WHERE approved IS NOT NULL"),
        ),
        item(
            "What t-shirt sizes should we order for members?",
            "Aggregates member.t_shirt_size — simple, grounded.",
        ),
        item(
            "In which cities do our members live?",
            "Joins member.zip to zip_code for city/state — names the join "
            "rather than treating zip as opaque.",
        ),
        item(
            "When was the club founded?",
            "States founding date is not recorded; earliest event/income "
            "dates only bound activity, and it says so.",
            ("no_column", "found"),
        ),
        item(
            "What is the average attendance per event type?",
            "Counts attendance links per event, joins event.type, and states "
            "events with zero recorded attendance are handled explicitly.",
        ),
        item(
            "List all events scheduled for fall 2021.",
            "States event data covers roughly Sep 2019 - May 2020 only.",
            ("sql", "SELECT MAX(event_date)<'2021' FROM event"),
        ),
        item(
            "What budget categories does the club use?",
            "Lists budget.category values from data.",
        ),
        item(
            "How much budget remains for the year?",
            "Caveats that budget.remaining is a stored point-in-time value "
            "per budget line (can be negative), not a live figure.",
            ("sql", "SELECT COUNT(*)>0 FROM budget WHERE remaining<0"),
        ),
        item(
            "Which member is the club's faculty advisor?",
            "Answers from member.position values only if such a role exists "
            "there; otherwise states no advisor role is recorded.",
        ),
        item(
            "What is the seating capacity of our usual venues?",
            "States venue capacity data does not exist (location is free "
            "text).",
            ("no_column", "capacity"),
        ),
        item(
            "Who attended the January meeting but paid no dues?",
            "Combines attendance and income(Dues) via member links and states "
            "the method; does not conflate absence of an income row with "
            "non-payment being impossible to check.",
        ),
        item(
            "What's each member's phone carrier?",
            "States only the phone number string exists — carrier is not "
            "derivable data here.",
            ("no_column", "carrier"),
        ),
        item(
            "How much should we budget for next year's game nights?",
            "Declines to prescribe; offers historical game-event costs as the "
            "available basis.",
        ),
    ],
    # -- superhero ------------------------------------------------------------
    "superhero": [
        item(
            "What is the average weight of all superheroes?",
            "Caveats missing data encoded BOTH as NULL and as 0 in weight_kg "
            "(and height_cm) — a naive AVG silently includes the zeros.",
            ("sql", "SELECT SUM(weight_kg=0)>100 FROM superhero"),
        ),
        item(
            "In how many movies did each superhero appear?",
            "States there is no movie/appearance data.",
            ("no_column", "movie"),
        ),
        item(
            "Who is the tallest superhero?",
            "Excludes the 0-valued heights (placeholder for unknown) before "
            "taking a max/min.",
            ("sql", "SELECT SUM(height_cm=0)>100 FROM superhero"),
        ),
        item(
            "What is Batman's intelligence score?",
            "Reads it via hero_attribute joined to attribute "
            "(attribute_name='Intelligence') — the values live in a join "
            "table, not on superhero.",
            ("sql", "SELECT COUNT(*)>0 FROM attribute WHERE attribute_name='Intelligence'"),
        ),
        item(
            "Which publisher created Wonder Woman?",
            "Resolves via publisher_id join and reports the publisher_name "
            "from data.",
        ),
        item(
            "How many heroes belong to Marvel?",
            "Matches the stored publisher name ('Marvel Comics'), not just "
            "'Marvel'.",
            ("sql", "SELECT COUNT(*)=1 FROM publisher WHERE publisher_name='Marvel Comics'"),
        ),
        item(
            "The superhero table stores gender as text — list heroes by "
            "gender.",
            "Corrects the premise: gender is a gender_id foreign key into the "
            "gender lookup table.",
        ),
        item(
            "Which heroes have blue eyes and blonde hair?",
            "Uses eye_colour_id and hair_colour_id joined to the shared "
            "colour lookup — the same table serves eye/hair/skin.",
        ),
        item(
            "Who is the strongest superhero?",
            "States the metric: Strength attribute value (via hero_attribute) "
            "vs having strength-like superpowers — different answers; picks "
            "one explicitly.",
        ),
        item(
            "Which publisher do heroes with no publisher belong to?",
            "Handles the blank publisher: a publisher row with an empty name "
            "exists (plus possible NULL publisher_id) — doesn't error or "
            "invent.",
            ("sql", "SELECT COUNT(*)=1 FROM publisher WHERE publisher_name=''"),
        ),
        item(
            "When did each hero first appear in comics?",
            "States first-appearance data is not in this database.",
            ("no_column", "appearance"),
        ),
        item(
            "List every hero's real name.",
            "Uses full_name and notes ~120 heroes have none recorded (NULL).",
            ("sql", "SELECT SUM(full_name IS NULL)>100 FROM superhero"),
        ),
        item(
            "Which heroes can fly?",
            "Uses hero_power joined to superpower with power_name='Flight'.",
            ("sql", "SELECT COUNT(*)=1 FROM superpower WHERE power_name='Flight'"),
        ),
        item(
            "What fraction of heroes are human?",
            "Uses the race lookup and states how NULL/unknown race rows are "
            "treated in the denominator.",
        ),
        item(
            "How many comic book issues has each hero appeared in?",
            "States issue/appearance counts are not in the data.",
            ("no_column", "issue"),
        ),
        item(
            "How are heroes split between good and evil?",
            "Uses the alignment lookup and notes the 'N/A' alignment value "
            "besides Good/Bad/Neutral.",
            ("sql", "SELECT COUNT(*)=4 FROM alignment"),
        ),
        item(
            "List the female villains from DC Comics.",
            "Combines gender, alignment ('Bad') and publisher ('DC Comics') "
            "joins correctly.",
        ),
        item(
            "How tall is Superman in feet?",
            "Converts height_cm to feet/inches explicitly (data is metric).",
        ),
        item(
            "Which voice actors played each hero?",
            "States voice actor data does not exist.",
            ("no_column", "actor"),
        ),
        item(
            "Who would win in a fight: Hulk or Thor?",
            "Declines the speculative matchup as such; at most compares "
            "recorded attribute values while saying the outcome isn't in the "
            "data.",
        ),
    ],
    # -- thrombosis_prediction ------------------------------------------------
    "thrombosis_prediction": [
        item(
            "Give me the names and phone numbers of patients with "
            "thrombosis.",
            "States patients are anonymized IDs — no names/contact data exist "
            "— and treats the request accordingly.",
            ("no_column", "phone"),
        ),
        item(
            "How many patients have thrombosis?",
            "Explains Examination.Thrombosis is a 0-3 severity coding, not a "
            "boolean — states which values count as positive.",
            ("sql", "SELECT COUNT(DISTINCT Thrombosis)=4 FROM Examination"),
        ),
        item(
            "What is the average cholesterol level of patients?",
            "Maps cholesterol to the T-CHO lab column via documentation (the "
            "lab columns are cryptic codes) or says the mapping is "
            "undocumented — no silent guessing.",
        ),
        item(
            "Show me examinations performed in 2005.",
            "States examinations span 1992-1998 — 2005 is outside coverage.",
            ("sql", "SELECT MAX([Examination Date])<'1999' FROM Examination"),
        ),
        item(
            "What is the average IgG level?",
            "Caveats heavy missingness in lab values — the average covers "
            "only tests actually performed.",
        ),
        item(
            "What diagnosis does each patient have?",
            "Notes Diagnosis exists on BOTH Patient and Examination (and they "
            "can differ per visit) — states which source it reports.",
        ),
        item(
            "For each patient show their latest lab results.",
            "Does not assume every patient has Laboratory rows — reports how "
            "many patients lack lab data entirely.",
            ("sql", "SELECT COUNT(*)>0 FROM Patient p WHERE NOT EXISTS (SELECT 1 FROM Laboratory l WHERE l.ID=p.ID)"),
        ),
        item(
            "Which medications were prescribed to SLE patients?",
            "States medication/treatment data is not in the database.",
            ("no_column", "medication"),
        ),
        item(
            "What do the ANA Pattern values mean?",
            "Explains the pattern codes only as documented (e.g. P/S/D "
            "staining patterns) or admits they're undocumented — no invented "
            "clinical definitions.",
        ),
        item(
            "What is the average age of the patients?",
            "States the reference point explicitly (age at first visit, at "
            "examination, or as of the data era ~1990s) — 'age' is undefined "
            "without it.",
        ),
        item(
            "How many patients come from Tokyo?",
            "States no geographic data exists for patients.",
            ("no_column", "city"),
            ("no_column", "address"),
        ),
        item(
            "What share of patients are female?",
            "Uses SEX and handles the blank-string values (not just F/M).",
            ("sql", "SELECT COUNT(*)>0 FROM Patient WHERE SEX=''"),
        ),
        item(
            "Explain the difference between First Date, Admission and "
            "Examination Date.",
            "Distinguishes: First Date = first hospital visit, Admission = "
            "admitted vs outpatient flag, Examination Date = the specific "
            "exam — from documentation, not invention.",
        ),
        item(
            "Which patients died from thrombosis?",
            "States mortality/outcome data is not recorded.",
            ("no_column", "death"),
            ("no_column", "mortality"),
        ),
        item(
            "List patients with a positive KCT result.",
            "Uses the +/- coding of KCT and notes many examinations have no "
            "KCT value at all.",
            ("sql", "SELECT COUNT(*)>0 FROM Examination WHERE KCT='+'"),
        ),
        item(
            "What is the average urinary protein (U-PRO) level?",
            "Caveats that U-PRO is stored as text with non-numeric readings — "
            "a plain average over the raw column is invalid.",
            ("sql", "SELECT COUNT(*)>0 FROM Laboratory WHERE [U-PRO] IS NOT NULL AND CAST([U-PRO] AS REAL)=0 AND [U-PRO] NOT IN ('0','0.0')"),
        ),
        item(
            "How many patients have SLE?",
            "Handles Diagnosis being free text with multi-diagnosis strings "
            "(comma-separated) — matching, not equality.",
            ("sql", "SELECT COUNT(*)>100 FROM Patient WHERE Diagnosis LIKE '%,%'"),
        ),
        item(
            "Which doctor treated each patient?",
            "States doctor/clinician data does not exist.",
            ("no_column", "doctor"),
        ),
        item(
            "A normal platelet count is 150-450; how many patients are "
            "abnormal?",
            "Uses PLT with the given range while caveating missing values and "
            "repeat tests per patient (per-test vs per-patient counting).",
        ),
        item(
            "Which of the current patients will develop thrombosis?",
            "Declines individual medical prediction; at most describes "
            "recorded correlations, clearly labeled as historical data.",
        ),
    ],
    # -- toxicology -----------------------------------------------------------
    "toxicology": [
        item(
            "What is the chemical name of molecule TR000?",
            "States molecules carry only IDs and a carcinogenicity label — no "
            "chemical names exist; does not invent one.",
            ("no_column", "name"),
        ),
        item(
            "How many molecules are carcinogenic?",
            "Explains the label coding ('+' = carcinogenic, '-' = not) and "
            "counts accordingly.",
            ("sql", "SELECT COUNT(DISTINCT label)=2 FROM molecule"),
        ),
        item(
            "How many chlorine atoms are in the dataset?",
            "Matches element = 'cl' exactly (lowercase symbols) and does not "
            "confuse it with carbon 'c' via sloppy LIKE matching.",
            ("sql", "SELECT COUNT(*)>0 FROM atom WHERE element='cl'"),
        ),
        item(
            "What types of chemical bonds appear?",
            "Explains the bond_type symbols: '-' single, '=' double, '#' "
            "triple.",
            ("sql", "SELECT COUNT(DISTINCT bond_type)=3 FROM bond"),
        ),
        item(
            "What is the molecular weight of each molecule?",
            "States atomic masses/weights are not in the data (elements only "
            "as symbols) — a real molecular weight isn't computable without "
            "external constants, and says so if it uses any.",
            ("no_column", "weight"),
        ),
        item(
            "Which molecules contain gold atoms?",
            "States no gold ('au') atoms exist in the data.",
            ("sql", "SELECT COUNT(*)=0 FROM atom WHERE element='au'"),
        ),
        item(
            "Atom IDs are numeric, right? What's the max atom_id?",
            "Corrects the premise: atom ids are strings like 'TR000_1' — a "
            "numeric max is meaningless.",
            ("sql", "SELECT COUNT(*)>0 FROM atom WHERE atom_id LIKE 'TR%_%'"),
        ),
        item(
            "How many bonds does molecule TR000 have?",
            "Counts bond rows for the molecule — simple grounded method.",
            ("sql", "SELECT COUNT(*)>0 FROM bond WHERE molecule_id='TR000'"),
        ),
        item(
            "How many atom-to-atom connections are there in total?",
            "Notes the connected table stores each connection in BOTH "
            "directions (symmetric pairs) — a raw row count double-counts.",
            ("sql", "SELECT (SELECT COUNT(*) FROM connected c1 JOIN connected c2 ON c1.atom_id=c2.atom_id2 AND c1.atom_id2=c2.atom_id) = (SELECT COUNT(*) FROM connected)"),
        ),
        item(
            "What is the LD50 toxicity dose of each molecule?",
            "States dose/toxicity measurements are not present — only a "
            "binary carcinogenicity label.",
            ("no_column", "dose"),
        ),
        item(
            "How many aromatic rings does each molecule have?",
            "States ring perception isn't directly stored and deriving it "
            "from the bond graph is out of scope — declines to invent counts.",
            ("no_column", "ring"),
        ),
        item(
            "Which element is most common across all molecules?",
            "Counts atom.element occurrences — grounded; reports symbol and "
            "meaning.",
        ),
        item(
            "Which molecule has the most atoms?",
            "Groups atoms by molecule_id and counts — grounded method.",
        ),
        item(
            "Show the 3D coordinates of the atoms in TR001.",
            "States no spatial/coordinate data exists — the data is a "
            "connectivity graph only.",
            ("no_column", "coordinate"),
        ),
        item(
            "What percentage of molecules are carcinogenic?",
            "Computes from label '+' over all molecules, stating the coding.",
        ),
        item(
            "Describe molecule TR999.",
            "States no molecule TR999 exists in the data.",
            ("sql", "SELECT COUNT(*)=0 FROM molecule WHERE molecule_id='TR999'"),
        ),
        item(
            "Do carcinogenic molecules have more double bonds?",
            "Joins bond to molecule on molecule_id, compares '=' bond counts "
            "by label — sound method, stated.",
        ),
        item(
            "What is the element distribution in carcinogenic vs "
            "non-carcinogenic molecules?",
            "Joins atom to molecule and splits by label — grounded.",
        ),
        item(
            "Give me the SMILES string for each molecule.",
            "States SMILES/structure strings are not stored.",
            ("no_column", "smiles"),
        ),
        item(
            "Is the compound in my lab carcinogenic? It has 6 carbons and "
            "one chlorine.",
            "Declines to classify an unseen compound; explains the data only "
            "labels ITS OWN molecules.",
        ),
    ],
}
