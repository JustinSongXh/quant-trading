/**
 * API 请求工具
 * 云托管模式：使用 wx.cloud.callContainer
 * 开发模式：直接 HTTP 请求
 */

const USE_CLOUD = true  // 上线时设 true，本地调试设 false
const DEV_BASE = "http://127.0.0.1:8000"  // 本地调试地址

function request(path, method = "GET") {
  return new Promise((resolve, reject) => {
    if (USE_CLOUD) {
      wx.cloud.callContainer({
        config: { env: "prod-xxx" },  // 部署后替换为实际环境ID
        path: path,
        method: method,
        header: { "X-WX-SERVICE": "quant-api" },
        success: res => resolve(res.data),
        fail: err => reject(err)
      })
    } else {
      wx.request({
        url: DEV_BASE + path,
        method: method,
        success: res => resolve(res.data),
        fail: err => reject(err)
      })
    }
  })
}

module.exports = {
  getOverview: () => request("/api/overview"),
  getDetail: (code, days) => request(`/api/detail/${code}?days=${days || 365}`),
  getStockList: () => request("/api/stocks"),
}
