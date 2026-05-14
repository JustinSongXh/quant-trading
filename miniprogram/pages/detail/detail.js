const api = require("../../utils/api")

Page({
  data: {
    code: "",
    info: null,
    loading: true,
    error: "",
    ecKline: { lazyLoad: true },
  },

  onLoad(options) {
    this.setData({ code: options.code || "" })
    this.fetchData()
  },

  async fetchData() {
    const { code } = this.data
    if (!code) return

    this.setData({ loading: true, error: "" })
    try {
      const info = await api.getDetail(code)
      this.setData({ info, loading: false })
      wx.setNavigationBarTitle({ title: `${info.name}(${info.code})` })

      // 延迟渲染图表
      setTimeout(() => this.renderKlineChart(), 300)
    } catch (e) {
      this.setData({ loading: false, error: "加载失败" })
    }
  },

  renderKlineChart() {
    const { info } = this.data
    if (!info || !info.kline || info.kline.length === 0) return

    this.selectComponent("#klineCanvas").init((canvas, width, height, dpr) => {
      const chart = require("../../ec-canvas/echarts.min")
      const myChart = chart.init(canvas, null, { width, height, devicePixelRatio: dpr })

      const kline = info.kline
      const dates = kline.map(k => k.date.slice(5))  // MM-DD
      const ohlc = kline.map(k => [k.open, k.close, k.low, k.high])
      const volumes = kline.map(k => k.volume)
      const ma5 = kline.map(k => k.ma5)
      const ma20 = kline.map(k => k.ma20)

      const option = {
        animation: false,
        grid: [
          { left: "10%", right: "2%", top: "5%", height: "55%" },
          { left: "10%", right: "2%", top: "68%", height: "25%" },
        ],
        xAxis: [
          { type: "category", data: dates, gridIndex: 0, axisLabel: { show: false } },
          { type: "category", data: dates, gridIndex: 1, axisLabel: { fontSize: 10 } },
        ],
        yAxis: [
          { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "#f0f0f0" } } },
          { scale: true, gridIndex: 1, splitLine: { lineStyle: { color: "#f0f0f0" } } },
        ],
        dataZoom: [{
          type: "inside", xAxisIndex: [0, 1],
          start: Math.max(0, 100 - 60 / kline.length * 100), end: 100
        }],
        series: [
          {
            name: "K线", type: "candlestick", data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
            itemStyle: { color: "#e74c3c", color0: "#27ae60", borderColor: "#e74c3c", borderColor0: "#27ae60" },
          },
          {
            name: "MA5", type: "line", data: ma5, xAxisIndex: 0, yAxisIndex: 0,
            lineStyle: { width: 1, color: "#2196F3" }, symbol: "none",
          },
          {
            name: "MA20", type: "line", data: ma20, xAxisIndex: 0, yAxisIndex: 0,
            lineStyle: { width: 1, color: "#E91E63" }, symbol: "none",
          },
          {
            name: "成交量", type: "bar", data: volumes, xAxisIndex: 1, yAxisIndex: 1,
            itemStyle: {
              color: function (params) {
                const k = ohlc[params.dataIndex]
                return k[1] >= k[0] ? "#e74c3c" : "#27ae60"
              }
            },
          },
        ],
      }

      myChart.setOption(option)
      return myChart
    })
  },
})
