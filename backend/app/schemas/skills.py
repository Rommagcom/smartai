from pydantic import BaseModel, Field


class SkillManifest(BaseModel):
    name: str
    title: str
    description: str
    version: str = "1.0.0"


class SkillContract(BaseModel):
    manifest: SkillManifest
    input_schema: dict
    permissions: list[str]


class SkillsRegistryResponse(BaseModel):
    registry_version: str
    skills: list[SkillContract]
    integration_tools: list[SkillContract] = Field(default_factory=list)
    dynamic_tool_operations: list[SkillContract] = Field(default_factory=list)
