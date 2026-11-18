from pydantic import BaseModel,Field,ConfigDict,field_validator,model_validator
class Inputs(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)
class SampleInput(Inputs):
    string_code:str=Field(min_length=1,max_length=50)
    current_a:float=Field(gt=0,le=1000)
    healthy:bool=False
    @field_validator('string_code')
    @classmethod
    def strip_code(cls,v):
        v=v.strip()
        if not v:raise ValueError('组串编号不能为空')
        return v
class DiagnosticInput(Inputs):
    code:str=Field(min_length=1,max_length=50)
    block_id:int
    samples:list[SampleInput]=Field(min_length=1,max_length=200)
    @field_validator('code')
    @classmethod
    def strip_code(cls,v):
        v=v.strip()
        if not v:raise ValueError('诊断编号不能为空')
        return v
    @model_validator(mode='after')
    def unique_codes(self):
        codes=[s.string_code for s in self.samples]
        if len(codes)!=len(set(codes)):raise ValueError('同一诊断内组串编号不能重复')
        return self
class RecheckInput(Inputs):
    samples:list[SampleInput]=Field(min_length=1,max_length=200)
    @model_validator(mode='after')
    def unique_codes(self):
        codes=[s.string_code for s in self.samples]
        if len(codes)!=len(set(codes)):raise ValueError('同一复诊内组串编号不能重复')
        return self
class RepairInput(Inputs):
    repair_note:str=Field(min_length=5,max_length=500)
    @field_validator('repair_note')
    @classmethod
    def note(cls,v):
        v=v.strip()
        if len(v)<5:raise ValueError('维修说明至少5字')
        return v
class FaultNote(Inputs):
    note:str=Field(min_length=5,max_length=500)
class FaultReportInput(Inputs):
    block_id:int
    strings:list[str]=Field(min_length=1,max_length=200)
    note:str=Field(min_length=5,max_length=500)
    @field_validator('strings')
    @classmethod
    def clean(cls,v):
        out=[]
        for item in v:
            item=item.strip()
            if not item:raise ValueError('异常组串编号不能为空')
            out.append(item)
        if len(out)!=len(set(out)):raise ValueError('异常组串编号不能重复')
        return out
    @field_validator('note')
    @classmethod
    def note_text(cls,v):
        v=v.strip()
        if len(v)<5:raise ValueError('登记说明至少5字')
        return v
