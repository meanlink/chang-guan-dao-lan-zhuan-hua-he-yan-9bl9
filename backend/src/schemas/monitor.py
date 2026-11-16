from pydantic import BaseModel, Field, model_validator, field_validator
class MonitorInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    zone: str = Field(min_length=2, max_length=80)
    baseline_job_id: int
    drop_threshold: float = Field(gt=0, le=100)          # 百分点，必须为正
    consecutive_limit: int = Field(ge=2, le=30)          # 连续异常天数门槛
    start_date: str = Field(min_length=10, max_length=10)
class FlowItem(BaseModel):
    arm: str = Field(max_length=4)
    visitors: int = Field(gt=0)
    completed: int = Field(ge=0)
    @field_validator('visitors', 'completed', mode='before')
    @classmethod
    def _reject_bool(cls, v):
        if isinstance(v, bool):
            raise ValueError('人数必须为整数，不能是布尔值')
        return v
class FlowInput(BaseModel):
    log_date: str = Field(min_length=10, max_length=10)
    expected_revision: int | None = Field(default=None, ge=1)  # 空=补报；带号=更正，按所见修订裁决
    items: list[FlowItem] = Field(min_length=2, max_length=2)
    @model_validator(mode='after')
    def _check_date_and_counts(self):
        from datetime import date as _date
        try:
            _date.fromisoformat(self.log_date)
        except ValueError:
            raise ValueError('客流日期格式须为 YYYY-MM-DD')
        for it in self.items:
            if it.completed > it.visitors:
                raise ValueError('完成讲解人数不能超过进区人数')
        return self
class ConfirmationInput(BaseModel):
    opinion: str = Field(min_length=2, max_length=1000)
