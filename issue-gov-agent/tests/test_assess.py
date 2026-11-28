"""评估决定的离线测试：不启动模型进程。"""

import unittest

import issue_gov.checks.tools.compliance.assess as assess
import issue_gov.checks.tools.compliance.comment as comment
from issue_gov.checks.tools.compliance.segment import segment_body


TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "description": "报告缺陷", "body": [
    {"type": "textarea", "attributes": {"label": "预期结果", "description": "期望行为"},
     "validations": {"required": True}},
    {"type": "dropdown", "attributes": {"label": "影响版本", "options": ["1.0", "2.0"]},
     "validations": {"required": True}}]}
SEGMENTS = [{"id": "S1", "kind": "heading", "text": "期望结果"},
            {"id": "S2", "kind": "paragraph", "text": "程序应正常退出"}]


def program(status="partial_match", problems=None, matched=None):
    problems = [] if problems is None else problems
    if matched is None:
        matched = ["缺陷反馈"] if status != "no_match" else []
    return {"status": status, "matched_templates": matched,
            "checks": [{"template": "缺陷反馈", "problems": problems}]}


class DecideReviewTests(unittest.TestCase):
    def test_exact_pass_still_rejects_wrong_template_and_ignores_conflicts(self):
        decision = assess.decide_review(program("pass", []), [TEMPLATE], {
            "template_fit": "wrong", "template": "缺陷反馈", "reason": "这是功能需求",
            "conflicts": [{"kind": "version_env", "left": "S1", "right": "S2", "note": "版本矛盾"}],
        }, SEGMENTS)
        self.assertEqual(decision["fallback"], "program")

    def test_wrong_template_stops_before_content_and_conflicts(self):
        other = {**TEMPLATE, "name": "功能需求", "title": "[Feature] "}
        decision = assess.decide_review(program("pass", []), [TEMPLATE, other], {
            "template_fit": "wrong", "template": "功能需求", "reason": "这是功能需求",
            "fields": [{"field": "预期结果", "status": "absent", "evidence": [], "ask": "请写明期望行为"}],
            "conflicts": [{"kind": "version_env", "left": "S1", "right": "S2", "note": "版本矛盾"}],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "no_match")
        self.assertEqual(decision["label_verdict"], "failed")
        self.assertEqual(decision["comment_kind"], "template")
        text = comment.format_decision(decision, templates=[TEMPLATE, other])
        self.assertIn("请改用模板 **功能需求**", text)
        self.assertIn("这是功能需求", text)
        self.assertIn("请写明期望行为", text)
        self.assertIn("版本矛盾", text)

    def test_synonym_and_unstructured_content_can_pass(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
            {"field": "title", "reason": "title_prefix", "message": "标题应以 '[Bug] ' 开头"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "answered", "evidence": ["S2"], "ask": None}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(decision["comment_kind"], "pass")

    def test_answered_field_still_counts_when_the_required_suffix_is_omitted(self):
        label = "Describe the current behavior / 问题描述 (Mandatory / 必填)"
        template = {**TEMPLATE, "body": [
            {"type": "textarea", "attributes": {"label": label, "description": "问题现象"},
             "validations": {"required": True}}]}
        for returned in (label, "Describe the current behavior / 问题描述"):
            with self.subTest(returned=returned):
                decision = assess.decide_review(program("partial_match", [
                    {"field": label, "reason": "missing_section", "message": "缺少此必填章节"},
                ]), [template], {
                    "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
                    "fields": [{"field": returned, "status": "answered", "evidence": ["S2"]}],
                    "conflicts": [],
                }, SEGMENTS)
                self.assertEqual(decision["label_verdict"], "passed")
                self.assertEqual(decision["comment_kind"], "pass")
        chinese = "问题描述（必填）"
        chinese_template = {**TEMPLATE, "body": [
            {"type": "textarea", "attributes": {"label": chinese, "description": "问题现象"},
             "validations": {"required": True}}]}
        decision = assess.decide_review(program("partial_match", [
            {"field": chinese, "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [chinese_template], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "问题描述", "status": "answered", "evidence": ["S2"]}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["label_verdict"], "passed")

    def test_missing_required_text_is_listed_without_old_heading_nit(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "absent", "evidence": [], "ask": "请写明期望行为"}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["label_verdict"], "failed")
        text = comment.format_decision(decision, templates=[TEMPLATE])
        self.assertIn("当前使用模板为 **缺陷反馈**，此处需要修改。", text)
        self.assertIn("#### 待补充内容", text)
        self.assertIn("请写明期望行为", text)
        self.assertNotIn("缺少此必填章节", text)

    def test_invalid_option_remains_a_structure_failure(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "影响版本", "reason": "invalid_option", "message": "请从模板候选项中选择"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [], "conflicts": [],
        }, SEGMENTS)
        text = comment.format_decision(decision, templates=[TEMPLATE])
        self.assertIn("#### 选项\n", text)
        self.assertIn("**影响版本**", text)
        self.assertEqual(decision["label_verdict"], "failed")

    def test_model_can_reject_text_the_program_already_accepted(self):
        decision = assess.decide_review(program("pass", []), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "absent", "evidence": [], "ask": "这里写的是环境，没有期望行为"}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "partial_match")
        self.assertEqual(decision["label_verdict"], "failed")
        self.assertEqual(decision["comment_parts"]["content"][0]["message"], "这里写的是环境，没有期望行为")

    def test_omitted_review_keeps_text_the_program_accepted(self):
        decision = assess.decide_review(program("pass", []), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [], "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["label_verdict"], "passed")

    def test_model_corrects_the_template_the_program_identified(self):
        """程序唯一认定了缺陷反馈、模型确认实际是功能需求时，以模型为准。"""
        other = {**TEMPLATE, "name": "功能需求", "title": "[Feature] "}
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [TEMPLATE, other], {
            "template_fit": "correct", "template": "功能需求", "reason": "这是功能需求",
            "fields": [{"field": "预期结果", "status": "answered", "evidence": ["S2"]}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(decision["comment_kind"], "pass")

    def test_model_rejection_of_the_identified_template_still_suggests_another(self):
        """程序认定缺陷反馈、模型说用错并指向功能需求时，仍建议改用。"""
        other = {**TEMPLATE, "name": "功能需求", "title": "[Feature] "}
        decision = assess.decide_review(program("pass", []), [TEMPLATE, other], {
            "template_fit": "wrong", "template": "功能需求", "reason": "这是功能需求",
            "fields": [], "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["comment_kind"], "template")
        self.assertEqual(decision["label_verdict"], "failed")
        self.assertIn("请改用模板 **功能需求**", comment.format_decision(decision, templates=[TEMPLATE, other]))

    def test_conflict_fails_after_template_is_accepted(self):
        decision = assess.decide_review(program("pass", []), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [], "conflicts": [{
                "kind": "version_env", "left": "S1", "right": "S2", "note": "环境与日志中的版本不一致"}],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "partial_match")
        self.assertIn("前后描述不一致", comment.format_decision(decision, templates=[TEMPLATE]))

    def test_invalid_evidence_cannot_mark_a_field_answered(self):
        for status in ("answered", "partial"):
            with self.subTest(status=status):
                decision = assess.decide_review(program("partial_match", [
                    {"field": "预期结果", "reason": "empty_section", "message": "此必填章节为空"},
                ]), [TEMPLATE], {
                    "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
                    "fields": [{"field": "预期结果", "status": status, "evidence": ["S9"]}],
                    "conflicts": [],
                }, SEGMENTS)
                self.assertEqual(decision["comment_parts"]["content"][0]["field"], "预期结果")

    def test_partial_with_evidence_counts_as_provided(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "empty_section", "message": "此必填章节为空"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "partial", "evidence": ["S2"], "ask": "可以再写具体一些"}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["assessment"]["fields"][0]["status"], "answered")

    def test_partial_with_evidence_counts_as_provided(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "empty_section", "message": "此必填章节为空"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "partial", "evidence": ["S2"], "ask": "可以再写具体一些"}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["assessment"]["fields"][0]["status"], "answered")

    def test_blank_label_and_duplicate_fields_are_ignored(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "  ", "status": "answered", "evidence": ["S2"]},
                       {"field": "预期结果", "status": "answered", "evidence": ["S2"]},
                       {"field": "预期结果", "status": "absent", "evidence": []}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(len(decision["assessment"]["fields"]), 1)

    def test_field_label_whitespace_is_normalized_before_matching(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": " 预期结果 ", "status": "answered", "evidence": ["S2"]}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["label_verdict"], "passed")

    def test_not_applicable_passes_without_a_comment(self):
        decision = assess.decide_review(program("no_match", [], matched=[]), [TEMPLATE], {
            "template_fit": "not_applicable", "template": None, "reason": "没有适用模板",
            "maintainer": "建议新增申请模板", "fields": [], "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(decision["comment_kind"], "none")
        self.assertEqual(decision["maintainer"]["suggestion"], "建议新增申请模板")

    def test_not_applicable_still_reports_an_internal_conflict(self):
        decision = assess.decide_review(program("no_match", [], matched=[]), [TEMPLATE], {
            "template_fit": "not_applicable", "template": None, "reason": "没有适用模板",
            "maintainer": "建议新增申请模板", "fields": [],
            "conflicts": [{"kind": "version_env", "left": "S1", "right": "S2", "note": "环境与日志中的版本不一致"}],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "no_match")
        self.assertEqual(decision["recommendation_status"], "not_applicable")
        self.assertEqual(decision["label_verdict"], "failed")
        self.assertEqual(decision["comment_kind"], "conflict")
        self.assertEqual(decision["maintainer"]["suggestion"], "建议新增申请模板")
        text = comment.format_decision(decision, templates=[TEMPLATE])
        self.assertIn("环境与日志中的版本不一致", text)
        self.assertNotIn("模板", text)
        self.assertNotIn("建议新增申请模板", text)

    def test_model_confirms_the_template_when_the_program_did_not_match_it(self):
        """程序没识别到模板、模型确认用对时，以模型为准并继续查完整性。"""
        decision = assess.decide_review(program("no_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ], matched=[]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "这是缺陷反馈",
            "fields": [{"field": "预期结果", "status": "answered", "evidence": ["S2"]}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(decision["comment_kind"], "pass")

    def test_model_confirmation_keeps_real_gaps_when_the_program_did_not_match(self):
        """程序没识别到模板、模型确认用对但字段确实缺失时，按缺项提示，不要求改用。"""
        decision = assess.decide_review(program("no_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ], matched=[]), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "这是缺陷反馈",
            "fields": [{"field": "预期结果", "status": "absent", "evidence": [], "ask": "请写明期望行为"}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "partial_match")
        self.assertEqual(decision["label_verdict"], "failed")
        text = comment.format_decision(decision, templates=[TEMPLATE])
        self.assertIn("当前使用模板为 **缺陷反馈**，此处需要修改。", text)
        self.assertIn("请写明期望行为", text)
        self.assertNotIn("请改用", text)

    def test_wrong_template_without_a_replacement_reports_only_the_conflict(self):
        decision = assess.decide_review(program("partial_match", [
            {"field": "预期结果", "reason": "missing_section", "message": "缺少此必填章节"},
        ]), [TEMPLATE], {
            "template_fit": "wrong", "template": None, "reason": "现有模板都不适合",
            "maintainer": "请增加申请类模板", "fields": [],
            "conflicts": [{"kind": "version_env", "left": "S1", "right": "S2", "note": "环境与日志中的版本不一致"}],
        }, SEGMENTS)
        self.assertEqual(decision["recommendation_status"], "not_applicable")
        self.assertEqual(decision["label_verdict"], "failed")
        self.assertEqual(decision["comment_kind"], "conflict")
        self.assertEqual(decision["maintainer"]["suggestion"], "请增加申请类模板")
        text = comment.format_decision(decision, templates=[TEMPLATE])
        self.assertIn("环境与日志中的版本不一致", text)
        self.assertNotIn("请改用", text)

    def test_no_template_conflict_is_separate_from_template_failure(self):
        reviewed = assess.decide_review(None, [], {
            "conflicts": [{"kind": "step_result", "left": "S1", "right": "S2", "note": "步骤和现象不是同一件事"}],
        }, SEGMENTS)
        self.assertEqual(reviewed["conflicts"][0]["kind"], "step_result")
        text = comment.format_decision(
            {"comment_kind": "conflict", "comment_parts": {"conflicts": reviewed["conflicts"]}})
        self.assertIn("有一处描述不一致", text)
        self.assertIn("步骤和现象不是同一件事", text)
        self.assertNotIn("模板", text)

    def test_missing_conflict_list_does_not_count_as_no_conflict(self):
        self.assertEqual(
            assess.decide_review(None, [], {"template_fit": "correct"}, SEGMENTS)["fallback"],
            "correctness")
        reviewed = assess.decide_review(None, [], {"conflicts": []}, SEGMENTS)
        self.assertIsNone(reviewed["fallback"])
        self.assertEqual(reviewed["conflicts"], [])

    def test_model_configured_accepts_either_key_form(self):
        self.assertFalse(assess.model_configured("", "model", "https://models.example"))
        self.assertTrue(assess.model_configured("key", "model", "https://models.example"))
        self.assertTrue(assess.model_configured("", "model", "https://models.example", ("key-a",)))
        self.assertFalse(assess.model_configured("key", "", "https://models.example", ("key-a",)))

    def test_unknown_template_name_on_a_wrong_fit_retries(self):
        decision = assess.decide_review(program(), [TEMPLATE], {
            "template_fit": "wrong", "template": "不存在的模板", "reason": "应该换模板",
            "fields": [], "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision, {"fallback": "program"})

    def test_several_completed_templates_keep_the_one_the_model_confirms(self):
        other = {**TEMPLATE, "name": "功能需求", "title": "[Feature] "}
        filled = {"status": "pass", "matched_templates": ["缺陷反馈", "功能需求"], "checks": [
            {"template": "缺陷反馈", "problems": []}, {"template": "功能需求", "problems": []},
        ]}
        decision = assess.decide_review(filled, [TEMPLATE, other], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "这是缺陷反馈",
            "fields": [{"field": "预期结果", "status": "answered", "evidence": ["S2"]}],
            "conflicts": [],
        }, SEGMENTS)
        self.assertEqual(decision["status"], "pass")
        self.assertEqual(decision["label_verdict"], "passed")
        self.assertEqual(decision["comment_kind"], "pass")
        self.assertIsNone(decision["recommendation_status"])

    def test_conflict_value_must_be_a_list(self):
        decision = assess.decide_review(program("pass", []), [TEMPLATE], {
            "template_fit": "correct", "template": "缺陷反馈", "reason": "当前模板适用",
            "fields": [{"field": "预期结果", "status": "answered", "evidence": ["S2"]}],
            "conflicts": {"kind": "version_env"},
        }, SEGMENTS)
        self.assertEqual(decision, {"fallback": "program"})
        self.assertEqual(
            assess.decide_review(None, [], {"conflicts": {"kind": "version_env"}}, SEGMENTS)["fallback"],
            "correctness")

    def test_model_output_without_a_fit_falls_back(self):
        self.assertEqual(assess.decide_review(program(), [TEMPLATE], {"template_fit": "maybe"}, SEGMENTS),
                         {"fallback": "program"})


class SegmentTests(unittest.TestCase):
    def test_nested_list_items_merge_into_one_segment(self):
        segments = segment_body("### 复现步骤\n1. 打开\n2. 登录\n   - 输入账号\n3. 白屏\n\n### 期望\n正常")
        self.assertEqual([s["kind"] for s in segments], ["heading", "list", "heading", "paragraph"])
        self.assertEqual(segments[1]["text"], "打开\n登录\n输入账号\n白屏")

    def test_code_headings_and_blank_bodies(self):
        self.assertEqual(segment_body(""), [])
        self.assertEqual(segment_body("   "), [])
        segments = segment_body("```log\nTraceback\n```\n> 引用")
        self.assertEqual([s["kind"] for s in segments], ["code", "paragraph"])
        self.assertEqual(segment_body("纯文本")[0]["kind"], "paragraph")

    def test_segments_are_capped(self):
        segments = segment_body("\n\n".join(f"段落 {i}" for i in range(150)))
        self.assertEqual(len(segments), 100)
        self.assertEqual(segment_body("x" * 5000)[0]["text"], "x" * 4000)


if __name__ == "__main__":
    unittest.main()
