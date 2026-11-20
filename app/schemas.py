"""HTTP 入参模型。

时间一律采用 UTC 整秒（Unix epoch 秒）；起始日期用 YYYY-MM-DD，按 UTC 解释。
``alarm_timestamps`` 省略（字段缺失）表示缺明细，显式传 ``[]`` 表示确无告警。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


class SliceIn(BaseModel):
    turbine_id: str = Field(..., min_length=1)
    slice_name: str = Field(..., min_length=1)
    version: int | None = Field(None, ge=1, description="不传则取当前最大版本号 + 1")
    # 省略 = 缺明细；null 同样视为缺明细；[] = 确无告警
    alarm_timestamps: list[int] | None = Field(
        default=None,
        description="触发告警的 UTC 整秒列表；省略/null=缺明细，[]=确无告警",
    )

    @field_validator("turbine_id", "slice_name")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v.strip()

    @field_validator("alarm_timestamps")
    @classmethod
    def _nonnegative(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(ts < 0 for ts in v):
            raise ValueError("告警时刻必须是 UTC 整秒（非负整数）")
        return v


class WorkOrderIn(BaseModel):
    order_no: str = Field(..., min_length=1)
    start_ts: int = Field(..., ge=0, description="预警起始 UTC 整秒（闭区间）")
    end_ts: int = Field(..., ge=0, description="预警结束 UTC 整秒（闭区间）")

    @field_validator("order_no")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("工单号不能为空")
        return v.strip()


class TrialIn(BaseModel):
    slice_id: int = Field(..., ge=1)
    start_date: str = Field(..., pattern=DATE_RE, description="回放起始 UTC 日期")
    notify_cap: int = Field(..., ge=1, le=100, description="通知上限 1~100 条")
    work_orders: list[WorkOrderIn] = Field(..., min_length=1)
