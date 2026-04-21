from __future__ import annotations


def chain_from_v_gene(v_gene: str | None) -> str:
    if not v_gene:
        return ""
    v = v_gene.upper()
    if v.startswith("IGHV"):
        return "H"
    if v.startswith("IGKV") or v.startswith("IGLV"):
        return "L"
    return ""


def region_from_v_gene(v_gene: str | None) -> str:
    if not v_gene:
        return ""
    v = v_gene.upper()
    if v.startswith("IGHV"):
        return "VH"
    if v.startswith("IGKV"):
        return "VK"
    if v.startswith("IGLV"):
        return "VL"
    return ""
