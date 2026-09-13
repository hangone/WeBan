from errors import WorkflowResult, WorkflowStatus


def test_combine_uses_message_from_more_severe_result() -> None:
    failed = WorkflowResult.failed_result("网络失败")
    incomplete = WorkflowResult.incomplete("因学习不完整跳过考试")

    result = failed.combine(incomplete)

    assert result.status is WorkflowStatus.FAILED
    assert result.message == "网络失败"


def test_combine_uses_later_message_when_later_result_is_more_severe() -> None:
    incomplete = WorkflowResult.incomplete("学习未完成")
    failed = WorkflowResult.failed_result("提交失败")

    result = incomplete.combine(failed)

    assert result.status is WorkflowStatus.FAILED
    assert result.message == "提交失败"


def test_combine_explicit_message_still_overrides_deciding_result() -> None:
    result = WorkflowResult.success("阶段完成").combine(
        WorkflowResult.incomplete("阶段不完整"),
        message="汇总信息",
    )

    assert result.message == "汇总信息"
