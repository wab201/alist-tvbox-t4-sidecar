module.exports = {
  init: () => "upload-demo-init-ok",
  getName: () => "Upload Demo",
  homeContent: () => ({
    class: [{ type_id: "all", type_name: "All" }],
    list: [{ vod_id: "upload-home", vod_name: "Upload Demo" }],
  }),
  categoryContent: (tid, pg) => ({
    page: Number(pg),
    list: [{ vod_id: `${tid}-${pg}`, vod_name: "Upload Category" }],
  }),
  detailContent: (ids) => ({
    list: [{
      vod_id: ids[0],
      vod_name: "Upload Detail",
      vod_play_from: "Upload Demo",
      vod_play_url: "Play$upload-play",
    }],
  }),
  searchContent: (key, quick, pg) => ({
    page: Number(pg),
    list: [{ vod_id: `search-${key}`, vod_name: key }],
  }),
  playerContent: (flag, id) => ({
    parse: 0,
    jx: 0,
    url: `https://video.invalid/${id}.m3u8`,
    header: { "User-Agent": "t4-upload-test" },
  }),
  action: (action) => ({ action, list: [] }),
};
