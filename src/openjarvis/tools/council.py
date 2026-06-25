"""Council tool — LLM Council pattern (Karpathy).

Runs a question through 5 independent advisors (distinct thinking lenses),
has them peer-reviewed anonymously, then a chairman synthesizes a final
verdict. Adapted from Andrej Karpathy's LLM Council: instead of different
models, it uses one (or more) engine(s) with different thinking-style system
prompts, fanned out in parallel via a thread pool.

Wiring: instantiated in ``JarvisSystem._build_tools`` like ``LLMTool`` because
it needs the engine + model injected. Follows the same pattern as
``openjarvis.tools.llm_tool.LLMTool``.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import Message, Role, ToolResult
from openjarvis.engine._stubs import InferenceEngine
from openjarvis.tools._stubs import BaseTool, ToolSpec

# ---------------------------------------------------------------------------
# The five advisors — thinking styles, not job titles. They are chosen to
# create natural tension: Contrarian vs Expansionist (downside vs upside),
# First-Principles vs Executor (rethink vs just-do-it), with the Outsider
# keeping everyone honest with fresh eyes.
# ---------------------------------------------------------------------------

_ADVISORS: Tuple[Tuple[str, str], ...] = (
    (
        "El Contrarian",
        "Buscas activamente lo que está mal, lo que falta, lo que va a fallar. "
        "Asumes que hay una falla fatal y la buscas. No eres pesimista: eres el "
        "amigo que evita un mal trato haciendo las preguntas que se están evitando.",
    ),
    (
        "El Pensador de Primeros Principios",
        "Ignoras la pregunta superficial y preguntas '¿qué estamos tratando de "
        "resolver realmente?'. Quitas supuestos y reconstruyes el problema desde "
        "cero. A veces lo más valioso es decir 'están haciendo la pregunta equivocada'.",
    ),
    (
        "El Expansionista",
        "Buscas el upside que todos los demás dejan pasar. ¿Qué podría ser más "
        "grande? ¿Qué oportunidad adyacente está escondida? No te importa el riesgo "
        "(ese es trabajo del Contrarian); te importa qué pasa si esto funciona aún "
        "mejor de lo esperado.",
    ),
    (
        "El Forastero",
        "No tienes NINGÚN contexto sobre quien pregunta, su campo ni su historia. "
        "Reaccionas solo a lo que tienes enfrente, sin jerga. Cazas la maldición del "
        "conocimiento: lo obvio para adentro pero confuso o injustificado para afuera.",
    ),
    (
        "El Ejecutor",
        "Solo te importa una cosa: ¿esto se puede hacer y cuál es el camino más "
        "rápido? Ignoras teoría y estrategia. Ves todo con el lente '¿qué haces el "
        "lunes en la mañana?'. Si algo suena brillante pero no tiene primer paso "
        "claro, lo dices.",
    ),
)

_LETTERS = ("A", "B", "C", "D", "E")


@ToolRegistry.register("council")
class CouncilTool(BaseTool):
    """Convene a 5-advisor LLM Council and synthesize a chairman verdict."""

    tool_id = "council"

    def __init__(
        self,
        engine: Optional[InferenceEngine] = None,
        *,
        model: str = "",
        chairman_model: str = "",
        max_workers: int = 5,
    ) -> None:
        self._engine = engine
        self._model = model
        # The chairman is the most consequential call; allow a stronger model.
        self._chairman_model = chairman_model or model
        self._max_workers = max_workers

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="council",
            description=(
                "Run a question, idea, or decision through a council of 5 AI "
                "advisors who independently analyze it from different angles, "
                "peer-review each other anonymously, and synthesize a final "
                "verdict. Use for high-stakes decisions with genuine uncertainty "
                "and multiple options — not for factual lookups or simple yes/no "
                "questions. Triggers: 'council this', 'war room this', "
                "'pressure-test this', 'stress-test this', 'debate this'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The decision or question to bring to the council. "
                            "Include the real options and what's at stake."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional extra context (constraints, numbers, "
                            "background) so advisors give grounded, specific advice."
                        ),
                    },
                    "include_transcript": {
                        "type": "boolean",
                        "description": (
                            "If true, append the full advisor responses and peer "
                            "review below the verdict. Default false (verdict only)."
                        ),
                    },
                },
                "required": ["question"],
            },
            category="reasoning",
            # 7 LLM calls (5 advisors + peer review + chairman); local models
            # are slow, so give it room.
            timeout_seconds=600.0,
            latency_estimate=120.0,
        )

    # -- internal helpers ---------------------------------------------------

    def _ask(self, system: str, prompt: str, *, model: str) -> str:
        """Single engine call → text. Raises on engine error."""
        messages = [
            Message(role=Role.SYSTEM, content=system),
            Message(role=Role.USER, content=prompt),
        ]
        result = self._engine.generate(messages, model=model)
        return (result.get("content") or "").strip()

    def _frame(self, question: str, context: str) -> str:
        framed = question.strip()
        if context.strip():
            framed += "\n\nContexto adicional:\n" + context.strip()
        return framed

    def _advisor_prompt(self, framed: str) -> str:
        return (
            "Un usuario trajo esta pregunta al council:\n\n---\n"
            f"{framed}\n---\n\n"
            "Responde desde tu perspectiva. Sé directo y específico. No matices "
            "ni busques equilibrio: inclínate totalmente a tu ángulo asignado. "
            "Los otros asesores cubren los ángulos que tú no cubres. Entre 150 y "
            "300 palabras, sin preámbulo. Responde en el MISMO idioma de la pregunta."
        )

    def _run_advisors(self, framed: str) -> List[str]:
        prompt = self._advisor_prompt(framed)

        def call(idx: int) -> str:
            name, style = _ADVISORS[idx]
            system = (
                f"Eres {name}, integrante de un LLM Council.\n"
                f"Tu estilo de pensamiento: {style}"
            )
            try:
                return self._ask(system, prompt, model=self._model)
            except Exception as exc:  # one advisor failing must not kill the council
                return f"[{name} no pudo responder: {exc}]"

        responses: List[str] = [""] * len(_ADVISORS)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as pool:
            futures = {pool.submit(call, i): i for i in range(len(_ADVISORS))}
            for fut in concurrent.futures.as_completed(futures):
                responses[futures[fut]] = fut.result()
        return responses

    def _peer_review(self, framed: str, responses: List[str]) -> str:
        anon = "\n\n".join(
            f"**Respuesta {_LETTERS[i]}:**\n{responses[i]}"
            for i in range(len(responses))
        )
        system = (
            "Revisas los resultados de un LLM Council de forma imparcial. "
            "Evalúas por mérito, no por estilo."
        )
        prompt = (
            "Cinco asesores respondieron, de forma independiente, a esta "
            f"pregunta:\n\n---\n{framed}\n---\n\n"
            f"Respuestas anonimizadas:\n\n{anon}\n\n---\n"
            "Responde, específico y por letra:\n"
            "1. ¿Cuál respuesta es la más fuerte y por qué?\n"
            "2. ¿Cuál tiene el mayor punto ciego y qué le falta?\n"
            "3. ¿Qué se les escapó a LAS CINCO que el council debería considerar?\n"
            "Máximo 220 palabras. Responde en el MISMO idioma de la pregunta."
        )
        try:
            return self._ask(system, prompt, model=self._model)
        except Exception as exc:
            return f"[Revisión por pares no disponible: {exc}]"

    def _chairman(self, framed: str, responses: List[str], review: str) -> str:
        named = "\n\n".join(
            f"**{_ADVISORS[i][0]}:**\n{responses[i]}" for i in range(len(responses))
        )
        system = (
            "Eres el Chairman de un LLM Council. Sintetizas el trabajo de 5 "
            "asesores y su revisión por pares en un veredicto final. Puedes "
            "disentir de la mayoría si el razonamiento lo respalda."
        )
        prompt = (
            f"La pregunta traída al council:\n\n---\n{framed}\n---\n\n"
            f"RESPUESTAS DE LOS ASESORES:\n\n{named}\n\n"
            f"REVISIÓN POR PARES:\n{review}\n\n---\n"
            "Produce el veredicto con esta estructura exacta (responde en el "
            "MISMO idioma de la pregunta):\n\n"
            "## Dónde coincide el council\n"
            "(Puntos en que varios asesores convergieron de forma independiente; "
            "señales de alta confianza.)\n\n"
            "## Dónde choca el council\n"
            "(Desacuerdos genuinos. Presenta ambos lados y por qué asesores "
            "razonables difieren. No los suavices.)\n\n"
            "## Puntos ciegos que cazó la revisión por pares\n"
            "(Lo que solo emergió en la revisión.)\n\n"
            "## La recomendación\n"
            "(Una recomendación clara y directa. No 'depende'. Una respuesta real.)\n\n"
            "## Lo único que hacer primero\n"
            "(Un solo siguiente paso concreto. No una lista.)\n\n"
            "Sé directo. No matices."
        )
        # Fall back to the main model — when deps are injected after __init__
        # (server path), _chairman_model may still be empty.
        return self._ask(system, prompt, model=self._chairman_model or self._model)

    # -- public API ---------------------------------------------------------

    def execute(self, **params: Any) -> ToolResult:
        if self._engine is None:
            return ToolResult(
                tool_name="council",
                content="No inference engine configured.",
                success=False,
            )
        if not self._model:
            return ToolResult(
                tool_name="council",
                content="No model configured.",
                success=False,
            )
        question = (params.get("question") or "").strip()
        if not question:
            return ToolResult(
                tool_name="council",
                content="No question provided.",
                success=False,
            )

        framed = self._frame(question, params.get("context") or "")
        try:
            responses = self._run_advisors(framed)
            review = self._peer_review(framed, responses)
            verdict = self._chairman(framed, responses, review)
        except Exception as exc:
            return ToolResult(
                tool_name="council",
                content=f"Council error: {exc}",
                success=False,
            )

        content = verdict
        if params.get("include_transcript"):
            transcript = "\n\n".join(
                f"### {_ADVISORS[i][0]}\n{responses[i]}"
                for i in range(len(responses))
            )
            content = (
                f"{verdict}\n\n---\n\n# Transcripción del council\n\n"
                f"{transcript}\n\n### Revisión por pares\n{review}"
            )

        return ToolResult(tool_name="council", content=content, success=True)


__all__ = ["CouncilTool"]
