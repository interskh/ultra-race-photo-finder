def site_link(platform: str | None, site_id: str | None, fname: str | None) -> dict | None:
    from photofinder.sources import pailixiang, photoplus, xxpie, yipai
    module = {"yipai": yipai, "pailixiang": pailixiang, "xxpie": xxpie, "photoplus": photoplus}.get(platform)
    return module.site_link(site_id, fname) if module and site_id else None
