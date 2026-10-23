from base.spider import Spider


class Spider(Spider):
    backend_parse = False

    def init(self, extend=""):
        self.extend = extend
        return "raw-init-ok"

    def getName(self):
        return "Raw Demo"

    def homeContent(self, filter):
        return {
            "class": [{"type_id": "all", "type_name": "All"}],
            "list": [{"vod_id": "demo-1", "vod_name": "Demo", "vod_pic": "https://img.invalid/demo.jpg"}],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "page": int(pg),
            "list": [{"vod_id": f"{tid}-1", "vod_name": "Category Demo"}],
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "Demo detail",
                    "vod_pic": "https://img.invalid/demo.jpg",
                    "vod_play_from": "Demo",
                    "vod_play_url": "正片$demo-play",
                }
            ]
        }

    def searchContent(self, key, quick, pg="1"):
        return {
            "page": int(pg),
            "list": [{"vod_id": f"search-{key}", "vod_name": key}],
        }

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "jx": 0,
            "url": f"https://video.invalid/{id}.m3u8",
            "header": {"User-Agent": "t4-test"},
        }

    def action(self, action):
        return {"action": action, "list": []}
