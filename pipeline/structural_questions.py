#!/usr/bin/env python3
"""Closed Jev vocabulary for structural affinity: social units and recurring story engines.

Jev is decision-only, so this deliberately generates no summary or tags. The stable question ids are the
vocabulary and the returned Noul probabilities are the vector. Multiple values may be true, which is why
these are independent Nouls rather than mutually-exclusive facet Choices.

A nine-title pilot for den-atlas#103 put The Middle's Malcolm in the Middle cosine at 0.918492, tied with
Modern Family at 0.918585 and above Gilmore Girls 0.869542, The King of Queens 0.659387, Succession
0.518721, The Wire 0.371503 and Game of Thrones 0.355325. A separate 500-title pilot returned all 500 rows;
no question fired above 0.5 on a majority of titles. That earns a corpus pass, not a ranking weight.
"""

STRUCTURAL = {
    "unit__family_household": (
        "Does the requested work center on the ongoing life of a family or household as its main social "
        "unit? The household must drive the story, not merely appear in a protagonist's background."
    ),
    "unit__parents_raising_children": (
        "Does the requested work substantially center on parents or caregivers raising children? Ordinary "
        "care, discipline and parent-child friction count; one rescue or custody event does not."
    ),
    "unit__sibling_group": (
        "Does an ongoing group of siblings and their relationships substantially drive the requested work? "
        "One incidental sibling or a single inheritance dispute does not."
    ),
    "unit__romantic_pair": (
        "Does an ongoing romantic couple, or the formation of one, serve as a principal social unit of the "
        "requested work?"
    ),
    "unit__friend_group": (
        "Does an ongoing group of friends and its internal relationships serve as a principal social unit "
        "of the requested work?"
    ),
    "unit__workplace_group": (
        "Does an ongoing workplace and its coworkers serve as a principal social unit of the requested "
        "work, rather than merely providing a character's occupation?"
    ),
    "unit__institutional_group": (
        "Does life inside an institution such as a school, prison, hospital, military unit or government "
        "body organize the requested work's recurring cast and situations?"
    ),
    "unit__team_or_crew": (
        "Does a team or crew organized around a shared task, mission, sport or enterprise serve as a "
        "principal social unit of the requested work?"
    ),
    "engine__everyday_domestic_problems": (
        "Are recurring ordinary household and family problems a principal story engine of the requested "
        "work? Money, chores, parenting, sibling conflict and minor domestic failures count; one exceptional "
        "family crisis does not."
    ),
    "engine__economic_precarity": (
        "Does limited money, insecure work or material hardship repeatedly constrain the characters' ordinary "
        "choices and generate stories in the requested work? A working-class label alone does not count."
    ),
    "engine__growing_up_and_school": (
        "Are growing up, school life, peer belonging or adolescent milestones a principal recurring story "
        "engine of the requested work?"
    ),
    "engine__workplace_problems": (
        "Are recurring work assignments, coworkers, clients or professional failures a principal story "
        "engine of the requested work? A character merely having a job does not count."
    ),
    "engine__relationship_formation": (
        "Is forming, maintaining or ending a romantic relationship a principal recurring story engine of "
        "the requested work?"
    ),
    "engine__case_or_investigation": (
        "Are cases, mysteries or investigations a principal recurring story engine of the requested work?"
    ),
    "engine__mission_or_quest": (
        "Are missions, quests or goal-directed expeditions a principal recurring story engine of the "
        "requested work?"
    ),
    "engine__survival_or_escape": (
        "Is surviving danger, confinement or disaster, or escaping it, a principal story engine of the "
        "requested work?"
    ),
    "engine__crime_scheme": (
        "Are committing, organizing or profiting from crimes or schemes a principal recurring story engine "
        "of the requested work?"
    ),
    "engine__status_and_power": (
        "Is competition for status, authority, succession or political power a principal recurring story "
        "engine of the requested work?"
    ),
}


def structural_questions():
    return {
        f"structural__{key}": {"type": "noul", "instructions": instructions}
        for key, instructions in STRUCTURAL.items()
    }


if __name__ == "__main__":
    import json

    qs = structural_questions()
    print(json.dumps({"count": len(qs), "ids": sorted(qs)}, indent=1))
