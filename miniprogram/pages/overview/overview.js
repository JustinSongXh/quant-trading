const api = require("../../utils/api")

Page({
  data: {
    aStocks: [],
    hkStocks: [],
    loading: true,
    error: "",
    updateTime: ""
  },

  onLoad() {
    this.fetchData()
  },

  onPullDownRefresh() {
    this.fetchData().then(() => wx.stopPullDownRefresh())
  },

  async fetchData() {
    this.setData({ loading: true, error: "" })
    try {
      const res = await api.getOverview()
      const stocks = res.stocks || []
      const aStocks = stocks.filter(s => s.market === "A")
      const hkStocks = stocks.filter(s => s.market === "HK")

      // 给每只股票加颜色标记
      const addColors = list => list.map(s => ({
        ...s,
        recColor: s.recommendation === "买入" ? "#e74c3c" : s.recommendation === "卖出" ? "#27ae60" : "#999",
        techColor: s.technical_signal > 0.1 ? "#e74c3c" : s.technical_signal < -0.1 ? "#27ae60" : "#666",
      }))

      this.setData({
        aStocks: addColors(aStocks),
        hkStocks: addColors(hkStocks),
        loading: false,
        updateTime: stocks.length > 0 ? stocks[0].date : ""
      })
    } catch (e) {
      this.setData({ loading: false, error: "加载失败，请下拉刷新重试" })
    }
  },

  goDetail(e) {
    const code = e.currentTarget.dataset.code
    wx.navigateTo({ url: `/pages/detail/detail?code=${code}` })
  }
})
