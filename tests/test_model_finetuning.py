from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "model-finetuning"
REFERENCE_PATH = "references/finetuning-method.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


def _reference() -> str:
    return next(
        template.content
        for template in builtin_skill_reference_templates()
        if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
    )


class ModelFinetuningCatalogTests(unittest.TestCase):
    """#1567: not training is an answer, and promotion needs the untuned baseline."""

    def test_the_decision_comes_first_and_not_training_is_an_outcome(self) -> None:
        mine = _definition(SKILL)
        self.assertEqual(mine.expected_outputs[0], "finetune_decision/v1")
        for schema in mine.expected_outputs:
            with self.subTest(schema=schema):
                self.assertEqual(len([line for line in mine.artifact_expectations if line.startswith(schema)]), 1)
        decision = next(line for line in mine.artifact_expectations if line.startswith("finetune_decision/v1"))
        self.assertIn("`do_not_finetune`", decision)
        self.assertIn("before any training step", decision)
        self.assertTrue(mine.safety_rules[0].startswith("Decide whether to fine-tune before any training step"))

    def test_promotion_requires_the_untuned_baseline_on_the_same_eval(self) -> None:
        mine = _definition(SKILL)
        self.assertTrue(mine.safety_rules[1].startswith("Never promote a checkpoint on a standalone score"))
        gate = next(line for line in mine.artifact_expectations if line.startswith("checkpoint_promotion_gate/v1"))
        self.assertIn("beats the untuned baseline", gate)
        comparison = next(line for line in mine.artifact_expectations if line.startswith("baseline_comparison/v1"))
        self.assertIn("same held-out eval on the untuned baseline and each candidate", comparison)

    def test_the_reference_names_a_data_shape_and_a_failure_mode_for_each_method(self) -> None:
        table = _reference().split("## 2. Choose the method", 1)[1].split("## 3.", 1)[0]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Method") and "---" not in line]
        self.assertEqual([row.split("|")[1].strip() for row in rows], ["SFT", "DPO", "RLVR"])
        for row in rows:
            with self.subTest(method=row.split("|")[1].strip()):
                self.assertTrue(all(cell.strip() for cell in row.strip("|").split("|")))
        ladder = _reference().split("## 1. Decide", 1)[1].split("## 2.", 1)[0]
        self.assertIn("`do_not_finetune`", ladder)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)

    def test_llm_app_dev_names_the_other_side(self) -> None:
        sibling = [text for text in _definition("llm-app-dev").do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(sibling), 1)
        mine = _definition(SKILL).do_not_use_when
        for other in ("model-optimization", "inference-serving", "llm-app-dev", "workflow-learning"):
            with self.subTest(sibling=other):
                self.assertEqual(len([text for text in mine if f"`{other}`" in text]), 1)


class ModelFinetuningRoutingTests(unittest.TestCase):
    def test_the_issue_row_and_the_finetuning_asks_dispatch_here(self) -> None:
        for message in (
            "sft vs dpo for our summarization model",
            "should we fine-tune a model or is prompting enough",
            "fine-tune llama on our support tickets with sft then dpo",
            "compare the tuned model against the untuned baseline before we promote the checkpoint",
            "should we use rlvr or dpo for the math model",
            "train a lora adapter on our internal docs",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_the_same_words_elsewhere_stay_away(self) -> None:
        for message in (
            "fine-tune the wording of this email",
            "we should fine tune our hiring process",
            "our dpo needs a report on the new privacy rules",
            "set up a lora gateway for the soil sensors",
            "my workout needs some fine-tuning",
            "the apartment is 900 sft with two bedrooms",
            "my model airplane needs a new adapter",
            "tune the guitar before the show",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_learning_from_a_run_stays_on_workflow_learning(self) -> None:
        route = _route("learn from this run so the model picks the right workflow next time")
        self.assertEqual(route["selected_skill"], "workflow-learning")

    def test_everyday_phrases_need_a_word_of_model_training(self) -> None:
        for message, withdrawn in (
            ("fine-tune the wording of this email", True),
            ("our dpo needs a report on the new privacy rules", True),
            ("set up a lora gateway for the soil sensors", True),
            ("is fine-tuning worth it for our classifier", False),
            ("sft vs dpo for our summarization model", False),
            ("train a lora adapter on our internal docs", False),
        ):
            with self.subTest(message=message):
                self.assertIs(everyday_sense_phrase_unanchored(SKILL, normalized_phrase(message)), withdrawn)


if __name__ == "__main__":
    unittest.main()
