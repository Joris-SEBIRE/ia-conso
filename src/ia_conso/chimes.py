"""Les deux sons de l'activité : une session qui a fini son tour, une qui attend ton intervention.

Un son se décide sur une transition, d'un sondage au suivant, et jamais sur un état : une session
qui attend depuis dix minutes ne resonne pas à chaque seconde. Une session n'est « finie » que si
le sondage la lit explicitement revenue au calme — une session qui disparaît le temps d'un fichier
lu à moitié n'a rien fini.
"""

from __future__ import annotations

from .activity import Activity

FINISHED = "finished"
ATTENTION = "attention"


class Bell:
    def __init__(self) -> None:
        # Sessions qui ont travaillé depuis leur dernier son : elles sonneront en rendant la main.
        self.armed: set[str] = set()
        # Sessions lues au dernier sondage, et qui n'attendaient rien.
        self.calm: set[str] = set()
        # Sessions lues au dernier sondage : une absence d'un seul sondage est un fichier lu à moitié.
        self.present: set[str] = set()
        # Coupures déjà vues, par session et par date : chacune ne sonne qu'une fois.
        self.halts: set[tuple[str, int]] = set()
        self.primed = False

    def feed(self, activity: Activity) -> set[str]:
        """Les sons que mérite ce sondage, comparé au précédent. Le premier ne sonne jamais."""
        sounds: set[str] = set()
        finished = (*activity.claude_finished, *activity.cursor_finished)
        # Une session au calme mais gardée à l'écran par une commande de fond n'est pas dans les
        # terminées : c'est sa ligne en cours qui dit si le tour a été coupé.
        ended = {item.key: item for item in (*activity.items, *finished)}
        if any(item.is_waiting and item.key in self.calm for item in activity.items):
            sounds.add(ATTENTION)
        # Une coupure peut tomber entre deux sondages sans qu'on ait vu la session travailler.
        halts = {(item.key, item.ended_ms) for item in finished if item.halt}
        if halts - self.halts:
            sounds.add(ATTENTION)
        self.halts |= halts
        self.armed |= {item.key for item in activity.items if item.is_busy}
        for key in self.armed & activity.settled:
            self.armed.discard(key)
            done = ended.get(key)
            if done is not None and getattr(done, "is_interrupted", False):
                continue
            # Une session coupée par une limite ou une erreur est à relancer : c'est à toi d'agir.
            sounds.add(ATTENTION if done is not None and getattr(done, "halt", "") else FINISHED)
        present = {item.key for item in activity.items} | activity.settled
        self.calm = {item.key for item in activity.items if not item.is_waiting} | activity.settled
        # Une session fermée en plein travail ne doit pas sonner si elle est rouverte plus tard.
        self.armed &= present | self.present
        self.present = present
        if not self.primed:
            self.primed = True
            return set()
        return sounds
