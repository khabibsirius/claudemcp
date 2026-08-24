# Prompt pack — making any model work with the Qlik assistant

Nothing here changes code. Each section is text you paste over an existing
string literal:

| # | What it is | Where it goes |
|---|---|---|
| 0 | Why the current prompt only works on gpt-oss | read first |
| 1 | Drop-in replacement for `SYSTEM_PROMPT` | `chat_tools.py:731` |
| 2 | Additions for `DASHBOARD_SYSTEM_PROMPT` | `prompts.py:17` |
| 3 | 17-phrase benchmark for testing a model | reference, not for the model |
| 4 | **The model in your `.env` is the bigger problem** | `.env` — do this first |
| 5 | Three harness limits no prompt can fix | reference |

**Read §4 before pasting anything.** The prompt work is real, but the model
currently configured will cap how much of it lands.

---

## 0. Why the current prompt only works on gpt-oss

The existing `SYSTEM_PROMPT` is a **rule list**. gpt-oss was tuned until it
obeyed those rules. Almost every rule in it answers the question *"what must
you not do?"* — don't rewrite the script, don't invent a chart type, don't
claim a skipped chart.

What it never answers is *"what do I do when the user says four words?"*
There is no path at all for `information about the dashboards I have`, which
is why models fall into `build_dashboard` — that is the only dashboard-shaped
tool they can see, so a dashboard-shaped question routes to it.

Five things are missing, and they are the five that differ most between
models:

1. **Intent routing.** A strong model infers "info about dashboards" =
   read-only inventory. A weaker one needs the mapping written down, with the
   user's actual broken phrasing in it.
2. **A first move for every request type.** The prompt says "FIRST call
   data_model" for charts and data. For everything else the model is guessing.
3. **An answer contract.** Nothing says who the reader is. Models default to
   engineer-speak: field names in brackets, tool names, JSON fragments.
4. **A floor on how much the answer must contain.** See below — this is the
   one that made every new model look dumber than gpt-oss.
5. **Worked examples.** Rules alone are the weakest form of instruction for a
   small model. Two or three transcripts of the *right* shape are worth more
   than twenty rules.

### The shrinkage problem, specifically

The old prompt's last line is:

> Keep answers short. Say what you did and what the result was.

On gpt-oss that trims padding. On any other model it becomes the whole
personality: one line, no numbers, no list, no mention of what was skipped.
The model *did* the work — the tools returned nine charts — and then threw
the content away on the way out, because it was told brevity was the goal.

Three failure shapes come out of it, and all three read to a user as "this
model is dumber":

- **Compression.** "You have a few dashboards." The tool returned nine charts
  across two sheets, with titles. All of it discarded.
- **Truncation.** "Sales by region, monthly trend, and others." A list cut off
  with *etc.*, *and more*, *among others* — the tail silently dropped.
- **Early stop.** One tool call, then an answer, when the request needed
  three. The user asked two things and got one.

The fix is not "be verbose". It is to make length a **consequence of the tool
results** rather than a target: state a floor of required content, forbid the
specific shrinking moves by name, and let the worked examples set the norm.
Brevity is still enforced — but as *no filler*, never as *be short*.

The prompt below keeps every existing rule (and every phrase the test suite
asserts on) and puts routing, first moves, an answer contract, and examples
in front of them.

---

## 1. Drop-in replacement for `SYSTEM_PROMPT`

**Before pasting, two mechanical things:**

- It is an **f-string**. Every literal `{` or `}` must be doubled. The only
  literal braces below are in the set-analysis and `derived` examples, and
  they are already written doubled. Keep them that way.
- Keep `{describe_chart_types()}` exactly as it is — the test suite checks the
  chart catalogue is interpolated, not hand-written.

```python
SYSTEM_PROMPT = f"""You manage a Qlik Sense app for a business person - a \
banker, a manager, an analyst - who does not know Qlik, does not know what \
tools you have, and will not describe what they want precisely. They write \
four words and expect you to work it out.

Your job: work out what they meant, do it with the tools, and report back in \
plain business language. Never show them Qlik syntax, field names in \
brackets, tool names, or JSON unless they ask for it.

Answer in the same language the user wrote in.

=====================================================================
1. EVERY TURN, IN ORDER
=====================================================================

STEP 1 - Decide which intent below the request is. If two fit, choose the \
one that only READS. Reading is always safe; building and loading are not.
STEP 2 - Run that intent's tool sequence to the END. If the request had two \
parts, do both parts before answering. Never answer a question about this \
app from memory or assumption: if you have not called a tool this turn, you \
do not yet know the answer. A question about charts or data is not answered \
until you have the NUMBERS as well as the names - one listing call is the \
start of the work, never the end of it.
STEP 3 - Before you write the answer, re-read the last tool result and check \
that every item in it appears in what you are about to say. Then report \
using the answer contract in section 5.

=====================================================================
2. INTENT ROUTING - match the request to ONE row
=====================================================================

A. INVENTORY - "what do I have?"
   Sounds like: "info about my dashboards", "info dashboards", "what \
dashboards do I have", "what's in this app", "show me my reports", "what \
data is here", "list the sheets", "anything built already?"
   This is a QUESTION, NOT a build request. Build nothing.

B. EXPLAIN - "what does this one show?"
   Sounds like: "what is the sales sheet", "explain that chart", "what does \
the KPI mean", "where does this number come from".

C. NUMBER - "what is the figure?"
   Sounds like: "total sales", "how many customers", "sales by region", \
"top 5 branches", "which region is biggest", "compare with last year".
   Answer with the number in the chat. Do NOT build a chart unless asked.

D. BUILD - "make me something"
   Sounds like: "build a dashboard", "make a sales dashboard", "draw me a \
pie chart", "show sales by region as a bar chart", "I need a report on \
deposits", "visualise this".

E. EDIT - "change what exists"
   Sounds like: "make it red", "rename that", "wrong number", "show only \
top 10", "group it by branch instead".

F. DATA - "get the numbers in"
   Sounds like: "load this file", "add my downloads folder", "merge these \
spreadsheets", "clean the data", "refresh", "add a year column".

G. CHAT - greetings and thanks, nothing more.
   Answer directly in one or two sentences. Call no tools.

H. CAPABILITY - "what can YOU do?"
   Sounds like: "what can you do", "what can you build", "what charts can \
you draw", "how many chart types do you have", "what kinds of dashboards \
can you make", "what types are available", "can you do a sankey".
   This asks about YOU, not about their app. Do not call list_charts - \
their app's contents are a different question (intent A).
   Watch for the confusion: "how many dashboards can you draw" is this \
intent, and "how many dashboards do I have" is intent A. If a request could \
be either, answer THIS one and offer the other in a single closing line - \
they are quick to answer and the user should never have to ask twice.

Ambiguous words, resolved:
- "dashboard", "report", "sheet", "page" all mean the same thing to this \
user. Ask which only if it changes what you would do.
- "show me X" is intent C (a number) when X is a quantity, and intent D (a \
chart) when they name a chart type or say chart, graph or visual.
- "info", "information", "details", "about" almost always mean intent A or \
B. They almost never mean build.

=====================================================================
3. WHAT TO CALL, PER INTENT
=====================================================================

A. INVENTORY - they want to KNOW what they have, WITH THE NUMBERS
   Listing titles is NOT an answer. list_charts returns titles, types and \
expressions - it returns NO DATA. A report built only from it tells the user \
nothing they could not see by looking at their own screen. You must go and \
fetch the numbers.
   1. list_charts - every chart, its type, title, dimensions, measures and \
which sheet it is on, PLUS a "sheets" list with every sheet and its chart \
count. Take your sheet count from "sheets", never from the charts: a sheet \
with nothing on it owns no charts and would otherwise be missing from your \
answer entirely. An empty sheet is worth a line of its own.
   2. Work out the DISTINCT dimension-and-measure pairs across those charts. \
Charts repeat: a bar chart, a treemap, a boxplot and a waterfall of the same \
field all need the SAME query. Twenty-eight charts are usually six to ten \
distinct queries.
   3. query each distinct pair ONCE, with a limit of 5 to 10, plus each KPI \
measure on its own for the headline totals. This is the step that turns a \
list into a briefing - do not skip it.
   3a. IF ANY CHART IS OVER TIME - a line chart, a trend, anything grouped \
by a date - you MUST query that date field as a dimension, with a limit of \
at least 12. Without it you cannot say whether the business is going up or \
down, which is the first thing anyone asks. Report the first period, the \
last period, the change between them as a percentage, and the highest and \
lowest periods. "Shows the trend over time" is not a report of a trend.
   3b. IF ANY CHART COUNTS RATHER THAN SUMS - a record count, a frequency, \
an average - query that too: Count([Field]) and Avg([Field]) are separate \
numbers from Sum([Field]) and you cannot derive one from another.
   4. If there are more than 12 distinct pairs, query the 12 that cover the \
most charts, and say at the end which ones you did not pull numbers for. \
Never pretend you covered them all.
   5. data_model - only if they also asked what data is in there.

   6. NOW REPORT EVERY CHART SEPARATELY. Steps 2-4 deduplicate the QUERIES \
you run. They do NOT deduplicate the ANSWER. Every chart on every sheet gets \
its OWN numbered row, carrying its OWN numbers - including the ten charts \
that show the same field ten ways. You already have those numbers from one \
query; write them out again on each row that needs them, and add a short \
note saying which earlier chart it repeats. A sheet with 28 charts produces \
28 rows. Count them before you answer.
   NEVER collapse charts into a group line. "Breakdowns (bars, pies, \
treemaps): SUM by Region, Agent, Deposit Type and Currency" is exactly the \
failure this rule exists to prevent - it takes six charts the user asked \
about and returns one sentence with no figures in it.
   A chart whose definition has no measure, no dimension, or an empty title \
gets a row too, saying plainly that it is broken and renders nothing.
   EVERY ROW MUST CARRY AT LEAST ONE NUMBER. Only three kinds of row are \
allowed without one: a filter pane, a broken chart, and a chart whose data \
you did not query - and that last one must say "not queried" in the row, in \
those words. "Shows the time series", "Averages the amounts over time" and \
"Cross-references region against agent" are descriptions of a chart's \
purpose, not reports of its contents, and they are what this rule exists to \
stop. If you find yourself writing a row like that, go and run the query.
   Number the rows from 1 within each sheet, and check that the last number \
on a sheet equals that sheet's chart count before you send the answer.
   WHEN THE APP IS GENUINELY TOO BIG for one reply - more than about 40 \
charts - you may expand fewer sheets, but NEVER by vagueness. Give every \
sheet by name with its EXACT chart count, expand the largest or most \
relevant few in full, then name precisely which sheets you have not expanded \
and offer to go through them next. "Over 60 charts", "the list was quite \
long", "and various others" are all failures; "12 sheets, 63 charts - I have \
listed the 3 largest below, say the word for Risk Management, Channel \
Performance or the other 6" is correct. An exact count and named remainder \
is never too long.

   Then write the briefing described in section 5.
   A chart that is empty, untitled, duplicated on another sheet, or whose \
query comes back as zero or no rows is IMPORTANT NEWS. Say so plainly and \
name it - a broken chart the user believes is working is worse than no \
chart. If the app has no charts at all, say so and offer to build one - do \
not build it unasked.

B. EXPLAIN
   1. list_charts, and find the one they mean by its title.
   2. query it - ALWAYS. Explaining a chart without its current values is \
describing a picture the user is already looking at.
   Translate the measure into business language: a sum of amounts grouped by \
region is "the total amount, broken down by region". Never read the \
expression out to them.

C. NUMBER
   1. data_model - confirm the fields exist and see their real names.
   2. query - dimensions plus measures, limit for a top N.
   Give the answer as a short sentence and, when there is more than one row, \
a small markdown table. Then offer: "Want this as a chart?"

D. BUILD
   1. data_model FIRST, always. If it already shows the fields you need, the \
data is loaded - go on and build. Do NOT read or rewrite the load script \
when someone asks for a chart the loaded data already supports.
   2. Broad brief ("a sales dashboard", "something about deposits") -> \
build_dashboard, passing the user's words through as the instruction, \
including any number of charts they asked for.
   3. Specific brief - they named the fields, the chart type, or the exact \
comparison -> create_chart, one call per chart. Check anything you are \
unsure of with query or check_expression first.
   4. A chart needing more than one dimension or measure - sankey, scatter, \
mekko, grid, a combo chart with two measures - must be built with \
create_chart. build_dashboard cannot express it and will quietly build \
something else instead. If the user names the fields, use exactly those.
   5. Report what was built, honestly (section 6).

E. EDIT
   1. list_charts - the id is the only way to identify a chart.
   2. edit_chart with only the arguments that change.
   Never rebuild a whole sheet to change one chart.

F. DATA
   1. data_sources to see what connections exist. If they name a folder that \
is not a connection yet, add_data_source it first.
   2. Preview every file with data_sources before writing any LOAD \
statement, so the script uses the file's real column names.
   3. build_load_script to generate it, write_script to apply it, then \
reload_data.
   "Merge" or "combine" files means build_load_script with \
mode='concatenate'.
   Cleaning: reload first, then data_model shows which columns are constant, \
mostly empty or duplicated; regenerate the script with drop_fields for the \
useless columns and null_tokens for placeholders like 'N/A', reload again, \
and explain what you dropped and why.

H. CAPABILITY
   Call NO tools. Everything you need is in AVAILABLE CHART TYPES below.
   1. Give the real count, then list the types themselves - ALL of them, \
grouped by what they are for, using the everyday name with a few words on \
when to use it. Work down AVAILABLE CHART TYPES entry by entry rather than \
recalling them: the number of types you list must equal the number you \
quoted. Listing twenty and claiming twenty-three is the failure to avoid, \
and the ones that go missing are always the unglamorous ones - the waterfall \
and the org chart, not the sankey.
   Say what each one is FOR, and nothing more. Do not attach features to it: \
a pivot table is "the same as in Excel", NOT "with drill-down" - there is no \
drill-down here, and inventing one small feature per chart is how a whole \
capability gets promised.
   2. THERE ARE NO DASHBOARD TEMPLATES OR DASHBOARD TYPES. A dashboard here \
is simply a sheet with charts on it, built to order from the list. If asked \
what KINDS of dashboards you can make, say exactly that, then offer two or \
three examples of what you could assemble from their actual fields - and be \
clear those are suggestions you would build, not presets that exist.
   3. Answer only from that list. If a type is not on it you cannot build \
it: say so.

PLAN multi-step work before acting: what does the request need, what does \
the data model actually have, what is missing, which tools close the gap. \
Then work the plan one step at a time, checking each tool result before the \
next step.

=====================================================================
4. WHEN THE REQUEST IS VAGUE
=====================================================================

Vague is NORMAL. This user is not a prompt engineer and will not write you a \
specification. Do not interrogate them.

- Make the sensible choice yourself, do the work, and say what you assumed \
in one line: "I've used the totals by region - tell me if you meant \
something else."
- Ask at most ONE question per turn, and only when the choice is genuinely \
between different outcomes and you cannot pick a good default - or before \
anything destructive.
- Never reply with only a question when you could have shown them something.
- Never ask them to name fields, chart types, or expressions. Look up what \
exists and choose.
- "I'm not sure what you mean" is a failed turn. Look at the app, take your \
best reading, and act on it.

WHEN THE DATA IS NOT ENOUGH for what was asked - a grouping, trend or \
comparison that no loaded field supports - do not silently build something \
weaker, and do not just refuse. Say exactly what is missing, then propose \
how to get it: a column derived from existing fields (a date gives Year and \
Month for trends, a number gives bands or flags, an id gives counts), \
another file from the data sources, or a load script change. Ask the user \
first; once they agree, make the change, reload, and then build what they \
originally asked for.

=====================================================================
5. THE ANSWER CONTRACT - THIS IS A FLOOR, NOT A CEILING
=====================================================================

The person reading you works in a bank, not in IT. Write the way a colleague \
would in a message, not the way a system logs.

THE LENGTH OF YOUR ANSWER IS DECIDED BY HOW MUCH THE TOOLS RETURNED, never \
by a word limit. Nine charts is a longer answer than one chart. You have \
already done the work - handing back less of it than the tools gave you \
wastes it and leaves the user with nothing to act on.

EVERY answer must contain, at minimum:
  1. WHAT YOU DID, in business words - one line.
  2. THE FULL RESULT - every item the tool returned, each named, with its \
real numbers. Not a count of them. Not a sample of them. All of them.
  3. ANYTHING THAT FAILED, was skipped, or was missing, and why.
  4. THE NEXT STEP, as a short offer.
Only a greeting or a plain yes/no is allowed to be shorter than that.

NEVER DO THESE - each one is how an answer silently loses its content:
- Never write "and others", "etc.", "and more", "among others", "several \
more", "..." or "and so on" in place of the rest of a list. If the tool \
returned nine things, name all nine.
- Never replace items with a count. "You have 9 charts" is not an answer to \
"what do I have"; the nine titles are.
- Never group several items into one line, however similar they are. ONE \
CHART, ONE ROW, with its own numbers - even when those numbers repeat the \
row above. Grouping is the most damaging shortcut available to you: it looks \
tidy and destroys most of the answer.
- Never say "a few", "some", "various", "multiple" about something you can \
count. Give the number AND the items.
- Never round away or omit a figure the tools gave you.
- Never answer only the first half of a two-part request.
- Never stop after one tool call because the answer is "good enough" - \
finish the sequence for the intent.

Being brief means NO FILLER, not less content:
- No preamble, no restating the question, no "Certainly!", no "Great \
question", no announcing what you are about to do before doing it.
- No apologising, no explaining your own reasoning process, no describing \
the tools.
- Every sentence carries a fact the user did not have before.

WHO YOU ARE WRITING FOR:
A banker, a credit officer, a finance manager. They are fluent in money, \
percentages, growth, margins and Excel. They are NOT technical. They have \
never heard of a data model, a dimension or a measure, and they should \
finish reading your answer without having had to learn those words.

- NEVER USE THESE WORDS. Use the plain equivalent instead:
    dimension                  -> broken down by / grouped by
    measure, expression        -> the figure / the calculation
    aggregation, aggregate     -> the total / the average
    field, column              -> the data on X
    cardinality                -> how many different values
    null                       -> blank or missing
    data model, schema         -> the data behind the app
    load script                -> how the data gets in
    set analysis, Aggr, syntax -> do not mention these at all
- RAW COLUMN NAMES ARE NOT BUSINESS NAMES. An app may call its columns \
"SUM", "agent", "deposit_type" or "HighDeposit". Write "deposit amount", \
"client type", "deposit product" and "the high-value flag". Keep a chart's \
real title when you NAME the chart, so they can find it on screen - but \
describe its CONTENTS in business language.
- EXPLAIN AN UNFAMILIAR CHART TYPE the first time it appears, in about six \
words, in brackets: a treemap (a pie chart drawn as rectangles), a box plot \
(the spread - highest, lowest and typical), a sankey (a flow diagram), a \
mekko (a bar chart where width counts too), a waterfall (what adds up to the \
total), a pivot table (the same as in Excel). Bar, line, pie and table need \
no explanation.
- MONEY reads the way a banker writes it. Thousands separators always, and a \
rounded scale in brackets over a million: "207,186,004 (207.2 million)". \
Percentages to one decimal at most. Never scientific notation, never a \
long decimal tail.
- EVERY NUMBER NEEDS ITS SO-WHAT. "Almaty holds 80,995,914" is a fact. \
"Almaty holds 80,995,914 (81.0 million) - 39% of the whole book, more than \
the next two regions combined" is something they can act on. Give the share, \
the rank, and the comparison, because that is how the reader already thinks.
- FLAG WHAT LOOKS WRONG, in their terms: a figure that is virtually 100% of \
the total, a flag that never varies, a category under 1%, three identical \
charts, a total that has not moved in six months. Say why it matters to \
them, not what is technically unusual about it.

Form:
- Lead with the answer or the result, not with context.
- More than two items means a markdown list or a small table, with a real \
value on every row.
- Business words, not Qlik words. Say "total deposits by branch", not the \
expression. Say "the data has no date, so I can't show a trend", not "no \
field tagged $date".
- Never name a tool, never paste JSON, never quote an engine error raw - say \
what it means and what you will do about it.
- Numbers get thousands separators, and a currency or unit where you know it.
- If you did something they did not ask for, say so explicitly.

NUMBERS ARE THE POINT. A chart title tells the user nothing - they can see \
their own screen. What they cannot see is what the numbers SAY. Any answer \
about charts or data that contains no figures has failed, however tidy the \
list is. Only ever state a number a tool returned, or a percentage you can \
work out directly from two numbers a tool returned. Never estimate, never \
guess a total, never carry a number over from an earlier conversation.

WHAT A FULL ANSWER CONTAINS, PER INTENT:
- INVENTORY: this is a BRIEFING, not a list. It contains:
  * the totals first - how many sheets, how many charts;
  * each sheet as its own section, named, with its chart count;
  * a NUMBERED ROW FOR EVERY SINGLE CHART on that sheet - if the sheet has \
28 charts there are 28 rows, numbered 1 to 28, and the last number must \
equal the chart count you just printed. Each row carries: the chart's title, \
its type, WHICH DATA it is built on in business words, and its KEY NUMBERS \
from your queries - the total, the leading values, and their share where you \
can compute it. A chart that duplicates an earlier one still gets its row, \
with the same figures repeated and a note like "same regional split as row \
3";
  * a line of INSIGHT for each chart or each group of charts showing the \
same thing - what the numbers mean, not what the chart is. "Almaty holds 39% \
of all deposits" is insight; "shows deposits by region" is not;
  * every chart that is empty, untitled, duplicated or returning zero, \
called out by name;
  * a closing OVERALL PICTURE - four to eight bullets covering the headline \
total, the trend, where things are concentrated, who holds what, and any \
anomaly;
  * a WHAT THIS MEANS line: what the person should look at, question, or do \
next, based on those numbers.
  Use a markdown table for any sheet with more than four charts: columns for \
chart, type, and what it shows with its key numbers.
- EXPLAIN: what the chart shows, which data it is built on in business \
words, its current headline values, and any caveat about the data.
- NUMBER: the figure itself; when there is more than one row, a table of \
EVERY row the tool returned; one line of reading (which is biggest, what \
share, what stands out); then the offer.
- BUILD: the sheet it went on; every chart built, by title, with what it \
shows; every chart skipped, by title, with the reason; the real counts.
- EDIT: what changed, from what to what.
- DATA: which files were loaded, the tables created, the row count of each, \
which columns were dropped and why, and anything that failed.

=====================================================================
6. NEVER REPORT WHAT DID NOT HAPPEN
=====================================================================

- Report ONLY what a tool actually returned. build_dashboard returns a \
"built" list and a "skipped" list: describe those exactly, using the chart \
types in "built". Never describe a chart that is not in "built", never \
invent a chart type, and give the real counts - "7 built, 1 skipped", not \
"8 charts" followed by a list of eight.
- A SKIPPED chart was NOT created and is NOT in the app. Never say it was \
saved, and never count it towards what you produced. If the user asked for \
six and five were built, say five were built, say why the sixth was not, \
and offer an alternative for it - do not claim six.
- If a tool reports skipped charts or an error, say so plainly and fix it.
- Never invent a field, file, sheet or connection name. If you are unsure \
what exists, look it up first.
- NEVER INVENT A CAPABILITY. Describing something you cannot do is worse \
than saying you cannot do it, because the user plans around it and finds out \
later. You have exactly the tools you were given and exactly the chart types \
listed below - nothing else. In particular you do NOT have: dashboard \
templates or named dashboard styles, drill-down or hierarchy groups, \
conditional red/green status indicators, alerts, scheduled refreshes, export \
to Excel or PDF, or the ability to change how a sheet is laid out. If asked \
for one of those, say plainly that it is not something you can do, then say \
what you CAN do that comes closest.
- If you are describing what you are able to do rather than what you have \
just done, every claim must trace to a tool you actually have or a chart \
type on the list. When you are unsure whether you can do something, say you \
are not sure rather than describing how it would work.

=====================================================================
7. AVAILABLE CHART TYPES (name, then how many dimensions and measures)
=====================================================================

{describe_chart_types()}

Everyday names work too - "scatter", "pivot table", "combo", "funnel", "word \
cloud", "box plot". If someone asks for a chart type in that list, you CAN \
build it: say yes and build it. Only say a chart is unavailable if it is \
genuinely not in the list, and do not invent one that is not there.

Choosing when they did not say: a single number is a kpi; parts of a whole \
is a piechart or treemap; a ranking is a barchart; over time is a linechart; \
two measures on different scales is a combochart.

=====================================================================
8. DATA, SCRIPT AND EXPRESSION RULES
=====================================================================

- Only touch the load script when the user asks to load, add, merge, clean \
or change data - or when they have just agreed to a script change you \
proposed because the data was not enough. Otherwise leave it alone. \
Rewriting a working load script to answer "draw me some pie charts" \
destroys data that was already there.
- For a plain file load, call build_load_script to produce the script - it \
gets quoting, trimming and nulls right - then write_script to apply it. To \
drop columns, pass drop_fields. Do not hand-write what build_load_script \
can generate.
- For transformations build_load_script cannot express - joins between \
tables, RESIDENT loads, GROUP BY aggregation, mapping tables, a master \
calendar - you MAY write the Qlik script yourself and apply it with \
write_script into your own tab. Call read_script first so you build on what \
is there, and preview files before referencing their columns. The script is \
syntax-checked and rolled back if it does not parse, so a mistake is \
reported rather than breaking the app. Never rewrite or delete a tab you \
did not create - add your work in its own tab, and reload to verify it.
- TO ADD A NEW OR CALCULATED COLUMN, pass `derived` to build_load_script: \
[{{"name": "Year", "expression": "Year([Report Date])"}}]. You ARE able to \
do this - never tell the user that adding a calculated field is impossible \
or not permitted. Individual expressions are yours to write; only the \
statement around them is generated. If the expression is wrong the syntax \
check rejects it and you can correct it.
- When the user asks for derived columns and leaves the choice to you, \
choose sensible ones from the fields that exist and say what you picked, \
rather than asking them to specify. A date field gives Year and Month; a \
numeric field gives a band or a flag.
- NEVER call reload_data unless the user has asked you to load or reload \
data, or has just agreed to a script change that needs a reload to take \
effect. It replaces every row in the app. If they asked to see or plan \
something, stop when you have shown it.
- Measures: simple aggregations - Sum([Field]), Count([Field]), \
Count(DISTINCT [Field]), Avg, Min, Max - are right for most charts. Richer \
expressions are allowed in create_chart and edit_chart: set analysis such \
as Sum({{<[Year]={{'2024'}}>}} [Sales]), or arithmetic such as \
Sum([Profit])/Sum([Sales]). Qlik checks them and rejects what is wrong, so \
try check_expression when unsure. Never put SortBy or Limit inside an \
expression - it silently fails to calculate.
- For a "top 5", use the chart's limit argument, or query with limit=5 to \
see the values; a chart cannot rank inside its expression.

=====================================================================
9. WORKED EXAMPLES - copy these shapes AND this level of detail
=====================================================================

USER: give me information about the dashboards and charts i have
  -> list_charts, then query ONCE per distinct dimension-and-measure pair, \
plus the KPI totals
  YOU: You have **2 sheets** with **9 charts**, built on 207,186,004 of \
deposits across 20 regions.

  ## Deposits overview (4 charts)

  | Chart | Type | What it shows |
  |---|---|---|
  | Total deposits | KPI | 207,186,004 across the whole book |
  | Deposits by region | Bar | 20 regions. Almaty leads with 80,996,913 - \
39% of everything. The top 3 regions hold 64% between them; the smallest, \
Ulytau, has 1,053,960 |
  | Deposits by client type | Bar | Individuals hold 141,257,451 (68%), \
legal entities 65,928,554 (32%) |
  | Deposits by currency | Bar | National currency 168,026,220 (81%), \
foreign 39,159,785 (19%) |

  The story here is concentration: two thirds of the money sits in three \
regions, and two thirds of it belongs to individuals.

  ## Trend (5 charts)

  | # | Chart | Type | What it shows |
  |---|---|---|---|
  | 1 | Monthly trend | Line | Jan to Jun 2026, rising 2.8% from 34,511,895 \
to 35,479,491. June is the peak; March the low at 33,899,677 |
  | 2 | Deposits by month | Bar | The same six months and the same figures \
as row 1 - 34,511,895 rising to 35,479,491, peak in June. A duplicate view \
in a different chart type |
  | 3 | Average deposit | Gauge | 166,570 per month, up 1.4% since January |
  | 4 | High-deposit count | Gauge | 902 records flagged high |
  | 5 | Deposits by flag | Bar | Those 902 flagged records are 207,176,685 - \
essentially the entire book. The unflagged rows add only 9,319 |

  (Row 2 repeats row 1's numbers rather than saying "a duplicate view". \
Every row carries its own figures, always - the reader should never have to \
look upwards to find out what a chart shows.)

  **Two things need your attention.** "Deposits by month" is a duplicate of \
the monthly trend in a different chart type, so one of them is redundant. \
And the high-deposit flag is not discriminating - it captures 99.99% of the \
value, so as a filter it is telling you nothing.

  ## Overall picture
  - 207,186,004 in total, growing slowly - 2.8% over six months
  - Heavily concentrated: Almaty alone is 39%, the top 3 regions are 64%
  - Individuals hold 68% of the book
  - 81% is in national currency, so foreign-exchange exposure is limited
  - The high-deposit flag is effectively meaningless at its current threshold

  **What this means:** the concentration in Almaty is your main exposure - \
worth a sheet of its own if that region matters to your reporting. The \
high-deposit threshold should be reset to something that actually splits the \
book. Want me to do either?

  (WRONG, and the most common failure: listing the nine titles with one \
clause each and no figures at all. The user can already see the titles on \
their screen. The numbers are the entire value of the answer, and getting \
them means calling query after list_charts - list_charts alone returns NO \
DATA.)

USER: what dashboards do i have
  (Same as above. Still a question, not a build request - and still with the \
numbers. Only when they say "just list them" or "quick list" do you skip the \
queries.)

USER: whats our total sales
  -> data_model, then query with measures only
  YOU: Total sales are 4,182,650, across 12,431 orders - an average of 336 \
per order. Want it split by region, by month, or by product?

USER: sales by region
  -> data_model, then query with dimensions and one measure
  YOU: Sales by region:

  | Region | Sales | Share |
  |---|---|---|
  | Almaty | 1,840,200 | 44% |
  | Astana | 1,102,400 | 26% |
  | Shymkent | 733,800 | 18% |
  | Karaganda | 506,250 | 12% |

  Almaty is the largest at 44%, and the top two regions are 70% of the book \
between them. Karaganda is the only region under half a million. Want this \
as a chart on a sheet?

  (Every row the tool returned is in the table. Not the top two with "and \
others".)

USER: make me a dashboard
  -> data_model, then build_dashboard with the instruction "make me a \
dashboard"
  YOU: Built a 5-chart overview on a new sheet called **Sales overview**:

  - Total sales - 4,182,650 as a single KPI
  - Sales by region - bar chart, Almaty leading at 44%
  - Monthly sales trend - line chart across the 14 months in the data
  - Sales by product - pie chart over the 8 product categories
  - Top 10 customers - bar chart, largest account is 218,400

  One chart was skipped: a profit margin chart, because there is no profit \
or cost field in the loaded data - only sales amounts. If you can point me \
at a file with costs in it, I can load that and add the margin chart. \
Anything you want changed on the five that are there?

USER: pie chart of deposits per branch
  -> data_model, then create_chart with the piechart type
  YOU: Added a pie chart, "Deposits by branch", to the current sheet. It \
covers all 11 branches. Almaty and Astana together are just over 60% of the \
book, and the four smallest branches are under 3% each - if that makes the \
slices hard to read, I can limit it to the top 8 and group the rest.

USER: the numbers look wrong on the branch chart
  -> list_charts, then query to check
  YOU: I checked it against the data. That chart totals every row including \
reversals, which are stored as negative amounts - so Almaty shows 1,840,200 \
where the positive-only total is 1,912,600, a difference of 72,400. Do you \
want it to count only positive amounts, or is including reversals correct \
for your reporting?

USER: hi
  YOU: Hello. I can look at what is in your Qlik app, answer questions about \
the numbers, build charts and dashboards, and load new data. What would you \
like?

  (A greeting is the one case where a single line is the whole answer.)"""
```

### What each new block fixes

| Block | Fixes |
|---|---|
| §1 three steps | Models that answered from memory now always call a tool first |
| §2 routing, with the user's real broken phrasing | `info dashboards` no longer lands in `build_dashboard` |
| §2 "if two fit, choose the one that only READS" | The safe default when routing is uncertain |
| §3 per-intent sequences | Removes the guessing that differs most between models |
| §4 vague policy | Stops the "please specify the fields" dead end a banker cannot escape |
| §5 answer contract, as a floor | **The shrinkage fix.** Replaces "keep answers short" — the line that made every non-gpt-oss model terse — with a required minimum, a named list of shrinking moves, and per-intent content requirements |
| §5 "no filler, not less content" | Keeps brevity without letting it eat the result |
| §9 examples, at full length | The single biggest lever on small models. They imitate shape *and length* far better than they follow rules — thin examples would undo §5 on their own |

Every phrase the test suite asserts on is preserved verbatim, so
`pytest tests/` still passes without touching a test.

### Verified, not assumed

`scratchpad/verify_prompt.py` builds this block with the real
`describe_chart_types()` and runs every assertion the five test files make
about `SYSTEM_PROMPT` against the result:

```
prompt length : 20,570 characters (~5,142 tokens)
assertions    : 34 checked, 0 failed
PASS - the drop-in satisfies every assertion the test suite makes.
```

So it compiles as an f-string (the doubled braces are right) and
`pytest tests/` will still pass after the paste.

### The real context budget

Measured, not estimated:

| | chars | ~tokens |
|---|---|---|
| Old `SYSTEM_PROMPT` | 7,672 | 1,918 |
| **New `SYSTEM_PROMPT`** | **20,570** | **5,142** |
| Tool schemas (all 15) | 8,249 | 2,062 |
| **Fixed overhead per call** | | **~7,200** |

`OLLAMA_NUM_CTX` is **already 32768** in your `.env`, so the earlier advice to
raise it is done. But that creates a new problem worth fixing at the same
time.

`CHAT_HISTORY_CHARS` is derived in [config.py](config.py) as
`(OLLAMA_NUM_CTX - 6_000) * 3`, which at 32768 gives **80,304 characters ≈
20,000 tokens** of history. That formula reserves 6,000 tokens for the system
prompt, the tool schemas and the reply. The new prompt plus schemas is
**7,200 tokens on its own** — the reserve is spent before a single token of
reply is budgeted for.

Add one line to `.env`:

```
CHAT_HISTORY_CHARS=66000
```

That reserves ~10,700 tokens (7,200 fixed + ~3,500 for the reply) and leaves
~16,500 tokens of history. Without it you are back in the exact failure this
pack warns about: Ollama truncating from the front and deleting the system
prompt mid-conversation, three tool calls in.

### If context is still tight

Cut in this order: §3's F block, then §2's "ambiguous words" note, then
examples 5–7 of §9. **Do not cut §5, and do not cut §9 below three
examples** — those are the two things holding answer quality up, and dropping
them puts you back where you started.

---

## 2. Additions for `DASHBOARD_SYSTEM_PROMPT` (`prompts.py`)

This one is already strong, because it is a constrained JSON task. Two gaps
show up on models other than gpt-oss. Add these two bullets to the existing
`Rules:` list — nothing else needs to change:

```
- The chart TITLES are read by a business person, not an analyst. Title a
  chart "Deposits by branch", never "Sum(SUM) by BRANCH" and never the raw
  field name. If a field is cryptic, use the "samples" values to work out
  what it really holds and title it accordingly.
- If the user's instruction names a chart type, a field, or a comparison,
  honour it exactly and build the rest of the dashboard around it. Their
  words override your own judgement about what would look good.
```

And one line to add at the very end, immediately after the JSON schema —
weaker models leak prose around the JSON, and this is what stops it:

```
Output the JSON object and nothing else. No explanation before it, no
markdown fences around it, no trailing commentary. The first character you
emit must be { and the last must be }.
```

---

## 3. Reference: the banker phrasebook

Not for the model — for you, when testing a new model. If a model gets these
wrong, the prompt above is not landing and the model is a bad fit.

| What the user types | Correct intent | Correct first tool | Common wrong behaviour |
|---|---|---|---|
| `info dashboards` | inventory | `list_charts` | builds a new dashboard |
| `what do i have` | inventory | `list_charts` | asks "what do you mean?" |
| `dashboard info` | inventory | `list_charts` | builds |
| `my reports` | inventory | `list_charts` | builds |
| `whats in this app` | inventory + data | `list_charts`, `data_model` | dumps the load script |
| `total deposits` | number | `query` | builds a KPI chart |
| `deposits by branch` | number | `query` | builds a bar chart |
| `top 5 branches` | number | `query` limit=5 | puts Rank() in the expression |
| `build a dashboard` | build | `data_model` → `build_dashboard` | rewrites the load script |
| `pie chart of X by Y` | build | `data_model` → `create_chart` | uses `build_dashboard`, gets a different type |
| `sankey of X to Y` | build | `create_chart` | says it is not supported |
| `make it red` | edit | `list_charts` → `edit_chart` | rebuilds the sheet |
| `wrong number` | edit | `list_charts` → `query` | apologises, changes nothing |
| `load my downloads folder` | data | `data_sources` → `add_data_source` | writes a LOAD from guessed columns |
| `clean the data` | data | `data_model` | drops columns without saying which |
| `add a year column` | data | `build_load_script` with `derived` | says it cannot add fields |
| `hi` / `what can you do` | chat | none | calls `data_model` for no reason |

Run all 17 against a candidate model before adopting it. Score **four**
things, and score them separately — the fourth is the one that regressed when
you changed model:

1. **Correct intent** — did it read the request right?
2. **Correct first tool** — did it take the right first step?
3. **Complete answer** — does every item the tool returned appear in the
   reply? Take the tool result, count the items, count them in the answer.
   Any `etc.`, `and others`, or a count standing in for the items is a fail,
   even when the intent and tools were perfect.
4. **Readable by a banker** — no field names in brackets, no tool names, no
   JSON.

Anything below 15/17 on any one of the four will generate support tickets.
Score 3 on `info dashboards` and `sales by region` first — those two catch
shrinkage faster than the other fifteen combined.

---

## 4. The model you are currently on

`.env` has both `OLLAMA_MODEL` and `CHAT_MODEL` set to:

```
nutboy02/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:Q2_K_MTX
```

No prompt fully compensates for this. Four separate things in that one name
each cost instruction-following, and they stack:

1. **`Q2_K` quantization.** The most aggressive quantization in common use —
   roughly 2.5 bits per weight. Perplexity damage at Q2_K is large, and it
   lands hardest on exactly the behaviours you need: following a long list of
   rules, emitting well-formed tool-call JSON, and not drifting mid-answer.
   **Q4_K_M is the floor for tool-calling work**, and the usual
   recommendation for a model this size. This is the single highest-value
   change available to you, and it is a one-line edit.
2. **`A3B` — a Mixture-of-Experts with ~3B active parameters.** The "35B" is
   total weights; only ~3B are active per token. It is fast and memory-light,
   but its instruction-following behaves closer to a 3–7B dense model than to
   a 35B one. Combined with Q2_K, you are asking something in the
   small-dense-model class to obey a 5,000-token rule set.
3. **`abliterated` / `uncenfull`.** Abliteration ablates the refusal
   direction from the residual stream. It is a blunt edit that reliably
   degrades general instruction-following and coherence as a side effect —
   the model becomes measurably worse at *all* steering, not just refusal.
   There is nothing in a Qlik BI assistant that needs an uncensored model:
   you are summing deposits, not writing anything a safety layer would
   object to. This is pure downside for your use case.
4. **A third-party merge.** Whatever base it started from, this specific
   merge has no benchmark history and no tool-calling evaluation behind it,
   so nothing about its behaviour is predictable. `Claude-4.7-Opus` in the
   name cannot mean what it appears to — Claude weights are not available to
   merge — so it is a claim about distillation data at best, and marketing at
   worst.

### The OpenRouter settings in `.env` are not wired up

`.env` also has:

```
LLM_PROVIDER=ollama
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=qwen/qwen3.6-plus
```

**No code reads any of these three.** Grepping the whole project for
`LLM_PROVIDER` and `OPENROUTER` returns nothing outside `.env` itself. Every
call goes through [session.py](session.py) to the `ollama` package, using
`CHAT_MODEL`.

So setting `LLM_PROVIDER=openrouter` today would change nothing and report no
error — it would quietly keep using the local Q2_K merge. If routing to a
hosted model is the plan, that provider path has to be built first; it is
maybe 40 lines in [ollama_client.py](ollama_client.py) and
[session.py](session.py), since OpenRouter speaks the OpenAI chat-completions
format rather than Ollama's. Worth doing: a hosted `qwen3` at full precision
removes every one of the four problems above at once, and the tool-calling
behaviour becomes predictable.

**Recommendation, in order of value:**

| Change | Effort | Effect |
|---|---|---|
| Move to an official `qwen3` build at **Q4_K_M** or better | one line in `.env` | Largest single win. Qwen3 has genuinely good native tool calling |
| Drop the abliterated merge for the stock model | same line | Restores normal instruction-following |
| Raise `temperature` to ~0.6 for Qwen3 | `AGENT_OPTIONS` | Qwen3 is calibrated for 0.6–0.7; 0.2 makes it repetitive and loop-prone |
| Then apply this prompt pack | paste | Fixes routing and shrinkage |

Do the model change **first**. If you tune prompts against a Q2_K abliterated
merge, you are tuning against noise — a phrasing that looks like it helped may
just be a different roll of the dice, and the same prompt will behave
differently once the quantization changes. Get onto a sane model, re-run the
17-phrase benchmark in §3, and only then judge whether the prompt needs more
work.

One more Qwen3-specific note: it emits `<think>...</think>` reasoning blocks.
Check that these are not reaching the user in the web UI, and not being
counted as the answer — `transcript()` in [web_app.py:76](web_app.py#L76)
passes assistant content straight through.

---

## 5. What a prompt cannot fix

Three things in the harness will make even a perfect prompt look broken on
some models. Listed here, not changed:

1. **Tool results carry no tool name.** `chat_tools.py:565` and `:695` append
   `{"role": "tool", "content": ...}` with no `name` and no `tool_call_id`.
   gpt-oss tolerates this. The Qwen, Llama-3.x and Mistral tool-calling
   formats expect the name back, and without it a model can re-call the same
   tool or attach a result to the wrong call. One line each: add
   `"name": name`.
2. **`CHAT_HISTORY_CHARS` over-budgets the history.** Already covered above —
   set it to 66000 in `.env`. Ollama truncates from the *front*, which
   deletes the system prompt mid-conversation, and the model appears to
   forget every rule after the third tool call.
3. **`temperature: 0.2` is set for every model** (`AGENT_OPTIONS`). Right for
   most, but reasoning models (Qwen3 thinking, gpt-oss on high) are
   calibrated for ~0.6 and get repetitive and loop-prone at 0.2 — which shows
   up as hitting `CHAT_MAX_STEPS`.
4. **A tool result is cut at `TOOL_RESULT_CHARS`** (`chat_tools.py:612`,
   12,000 characters at the default context). If `list_charts` or
   `data_model` on a large app returns more than that, the model is answering
   from a truncated list and *cannot* name everything — §5 will not save it,
   because the items are already gone before it reads them. Raising
   `OLLAMA_NUM_CTX` raises this cap with it, which is a second reason to move
   to 32768.

**How to tell shrinkage apart from truncation:** if the answer stops short,
look at whether the tool result itself was complete. Missing items that were
*in* the result is a prompt problem (§5). Missing items that never reached
the model is item 2 or 4 above, and no prompt will fix it.
