#!/usr/bin/env python3
"""What a stage needs from outside the machine: HTTP, the response cache, and the two upstreams.

A stage decides WHICH titles to ask about; these modules know how to ask. The split exists because the
answer to "how do I reach Wikidata politely" is the same for every stage that reaches it, and the seam
where it was answered twice is where the two answers drifted — `ResponseCache`'s key has one definition
here (`lib/cache.py`) and porting it a second time would throw 2.1 GB of cached bodies away silently.

Nothing here is a stage and nothing here reads the artifact catalogue: a module in `lib/` may be imported
by any stage and imports none of them.
"""
