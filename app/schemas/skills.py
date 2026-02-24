from pydantic import BaseModel


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
