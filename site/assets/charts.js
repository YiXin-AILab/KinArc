/* LongArc site charts — ECharts 动态渲染,数据来自 aritcle/指标表.xlsx 与进化轨迹。
 * 依赖 assets/echarts.min.js;由 index.html 的语言切换联动重渲染。 */
(function () {
  "use strict";

  var BLUE = "#2563eb";
  var TEAL = "#2ba88f";
  var RED = "#e5617a";
  var GRAY = "#b8bfcc";
  var INK = "#16233a";
  var MUTED = "#5b6472";
  var LINE = "#e6e2d9";
  var SANS = '-apple-system, BlinkMacSystemFont, "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';
  var REDUCED = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var NARROW_QUERY = "(max-width: 640px)";

  function isNarrow() {
    return window.matchMedia && window.matchMedia(NARROW_QUERY).matches;
  }

  // 窄屏下使用过短的配置名会截断,改用短名;数值本身不变化。
  var SHORT_NAMES = [
    "qwen3.8 × bm25",
    "qwen3.8 × golden",
    "deepseek-v4 × bm25",
    "deepseek-v4 × golden",
  ];

  function cfgName(cfg, narrow) {
    if (!narrow) return cfg.name;
    var i = CONFIGS.indexOf(cfg);
    return i >= 0 ? SHORT_NAMES[i] : cfg.name;
  }

  var CONFIGS = [
    {
      name: "qwen3.8-27B × bm25",
      base: { pass: [0.436, 0.328, 0.271, 0.243], train: 0.4706, val: 0.3478, test: 0.300, reward: 0.436 },
      cand: { pass: [0.598, 0.509, 0.446, 0.402], train: 0.7941, val: 0.4565, test: 0.55, reward: 0.5995 },
    },
    {
      name: "qwen3.8-27B × golden_retrieval",
      base: { pass: [0.558, 0.497, 0.454, 0.423], train: 0.5588, val: 0.4457, test: 0.35, reward: 0.5596 },
      cand: { pass: [0.637, 0.562, 0.513, 0.474], train: 0.7941, val: 0.6413, test: 0.625, reward: 0.6366 },
    },
    {
      name: "deepseek-v4-flash-0713 × bm25",
      base: { pass: [0.339, 0.255, 0.216, 0.195], train: 0.3529, val: 0.2174, test: 0.35, reward: 0.349 },
      cand: { pass: [0.421, 0.365, 0.342, 0.337], train: 0.5588, val: 0.337, test: 0.4, reward: 0.442 },
    },
    {
      name: "deepseek-v4-flash-0713 × golden_retrieval",
      base: { pass: [0.591, 0.536, 0.503, 0.484], train: 0.6176, val: 0.5761, test: 0.425, reward: 0.5951 },
      cand: { pass: [0.663, 0.606, 0.564, 0.533], train: 0.7941, val: 0.6304, test: 0.6, reward: 0.674 },
    },
  ];

  var EVO_CONFIGS = [
    {
      nameKey: "evoOffice",
      base: { train: 0.7806, val: 0.7917, test: 0.4667, reward: 0.6857 },
      cand: { train: 0.8696, val: 0.8333, test: 0.5667, reward: 0.7238 },
    },
    {
      nameKey: "evoRetrieval",
      base: { train: 0.339, val: 0.25, test: 0.185, reward: 0.24 },
      cand: { train: 0.4661, val: 0.6111, test: 0.338, reward: 0.481 },
    },
  ];

  var TREND = {
    stamps: ["09/19 23:09", "09/20 01:04", "09/20 03:45", "09/20 06:27",
      "09/20 08:28", "09/20 10:43", "09/20 11:42", "09/20 18:10",
      "09/20 22:37", "09/21 00:27", "09/21 04:11", "09/21 11:56"],
    train: [0.471, 0.471, 0.471, 0.559, 0.559, 0.441, 0.559, 0.588, 0.471, 0.588, 0.647, 0.735],
    val: [0.261, 0.261, 0.217, 0.337, 0.283, null, 0.413, 0.424, null, 0.413, 0.478, null],
  };

  function text(lang) {
    var zh = lang !== "en";
    return {
      base: zh ? "Base(原始版本)" : "Base (original)",
      cand: zh ? "自进化后(candidate)" : "Evolved (candidate)",
      splits: zh ? ["训练集", "验证集", "测试集", "Reward"] : ["Train", "Val", "Test", "Reward"],
      kAxis: zh ? "k(采样次数)" : "k (samples)",
      rewardTitle: zh ? "Reward 得分:四组配置自进化前后对比" : "Reward score: before vs after evolution",
      heatTitle: zh ? "自进化提升热力图:4 配置 × Pass^1–4 = 16 项对比全部改善(pp)"
        : "Improvement heatmap: all 16 comparisons improved (pp)",
      heatHigh: zh ? "提升大" : "High",
      heatLow: zh ? "提升小" : "Low",
      trendTitle: zh ? "tau3_banking_t34v23 自进化轨迹(12 个候选,线性链)"
        : "tau3_banking_t34v23 evolution trend (12 candidates, linear chain)",
      trendTrain: zh ? "Train(34 cases) 训练集" : "Train (34 cases)",
      trendVal: zh ? "Validation(23 cases) 验证集" : "Validation (23 cases)",
      trendBest: zh ? "Train best-so-far 训练集历史最佳" : "Train best-so-far",
      trendTrainS: zh ? "训练集" : "Train",
      trendValS: zh ? "验证集" : "Val",
      trendBestS: zh ? "历史最佳" : "Best",
      gainLabel: zh ? "提升" : "gain",
      tau3Name: "τ³-bench",
      evoName: "EvoAgentBench",
      overviewTauScope: zh ? "4 组模型 × 检索配置均值" : "Mean of 4 model × retrieval configurations",
      overviewEvoScope: zh ? "2 个场景均值" : "Mean of 2 scenarios",
      evoOffice: zh ? "办公场景" : "Office",
      evoRetrieval: zh ? "信息检索场景" : "Information retrieval",
    };
  }

  function baseGrid() {
    return {
      animation: !REDUCED,
      aria: { enabled: true },
      textStyle: { fontFamily: SANS, color: MUTED },
      tooltip: { trigger: "axis", confine: true },
      grid: { left: 12, right: 20, top: 44, bottom: 8, containLabel: true },
    };
  }

  function axisCommon() {
    return {
      axisLine: { lineStyle: { color: LINE } },
      axisTick: { show: false },
      axisLabel: { color: MUTED, fontSize: 10.5 },
      splitLine: { lineStyle: { color: LINE, opacity: 0.7 } },
    };
  }

  function pp(v) {
    return "+" + (v * 100 + 1e-8).toFixed(1) + "pp";
  }

  function metricMean(configs, version, metric) {
    var total = configs.reduce(function (sum, cfg) {
      return sum + cfg[version][metric];
    }, 0);
    return total / configs.length;
  }

  function benchmarkProfile(configs) {
    var metrics = ["train", "val", "test", "reward"];
    var base = metrics.map(function (metric) {
      return metricMean(configs, "base", metric);
    });
    var cand = metrics.map(function (metric) {
      return metricMean(configs, "cand", metric);
    });
    return { metrics: metrics, base: base, cand: cand };
  }

  function radarIndicators(t, profile) {
    return t.splits.map(function (label, i) {
      return { name: label + "\n" + pp(profile.cand[i] - profile.base[i]), max: 1 };
    });
  }

  /* ---- 双基准总览：两个独立的进化指纹，只比较各自 Base → Evolved ---- */
  function benchmarkOverviewOption(t, narrow) {
    var groups = [
      { name: t.tau3Name, scope: t.overviewTauScope, profile: benchmarkProfile(CONFIGS) },
      { name: t.evoName, scope: t.overviewEvoScope, profile: benchmarkProfile(EVO_CONFIGS) },
    ];
    var opt = baseGrid();
    opt.title = groups.map(function (group, i) {
      return {
        text: group.name,
        subtext: group.scope + " · Reward " +
          pp(group.profile.cand[3] - group.profile.base[3]),
        left: narrow ? "50%" : (i === 0 ? "25%" : "75%"),
        top: narrow ? (i === 0 ? 4 : 330) : 4,
        textAlign: "center",
        textStyle: { color: INK, fontSize: narrow ? 14 : 17, fontWeight: "bold" },
        subtextStyle: { color: MUTED, fontSize: narrow ? 10 : 11.5, lineHeight: 20 },
      };
    });
    opt.legend = {
      bottom: 0, left: "center", selectedMode: false,
      itemWidth: 13, itemHeight: 10, itemGap: 24,
      textStyle: { color: MUTED, fontSize: narrow ? 10 : 11.5 },
      data: [
        { name: t.base, icon: "circle" },
        { name: t.cand, icon: "diamond" },
      ],
    };
    opt.tooltip = {
      trigger: "item", confine: true,
      formatter: function (params) {
        var group = groups[params.seriesIndex];
        var lines = ["<b>" + group.name + "</b>", group.scope];
        t.splits.forEach(function (label, i) {
          lines.push(label + ": " + group.profile.base[i].toFixed(3) + " → " +
            group.profile.cand[i].toFixed(3) + " (" +
            pp(group.profile.cand[i] - group.profile.base[i]) + ")");
        });
        return lines.join("<br/>");
      },
    };
    opt.radar = groups.map(function (group, i) {
      return {
        indicator: radarIndicators(t, group.profile),
        center: narrow
          ? ["50%", i === 0 ? "24%" : "74%"]
          : [i === 0 ? "25%" : "75%", "57%"],
        radius: narrow ? "48%" : "60%",
        shape: "polygon", splitNumber: 4,
        axisNameGap: narrow ? 7 : 11,
        axisName: {
          color: INK, fontSize: narrow ? 9.5 : 11,
          lineHeight: narrow ? 14 : 16,
        },
        axisLine: { lineStyle: { color: "rgba(91,100,114,0.24)" } },
        splitLine: { lineStyle: { color: "rgba(91,100,114,0.20)" } },
        splitArea: {
          areaStyle: { color: ["rgba(37,99,235,0.018)", "rgba(37,99,235,0.045)"] },
        },
      };
    });
    opt.series = groups.map(function (group, i) {
      return {
        name: group.name, type: "radar", radarIndex: i,
        symbolSize: narrow ? 5 : 6,
        data: [
          {
            value: group.profile.base, name: t.base, symbol: "circle",
            lineStyle: { color: GRAY, width: 2 }, itemStyle: { color: GRAY },
            areaStyle: { color: GRAY, opacity: 0.10 },
          },
          {
            value: group.profile.cand, name: t.cand, symbol: "diamond",
            lineStyle: { color: BLUE, width: 2.6 }, itemStyle: { color: BLUE },
            areaStyle: { color: BLUE, opacity: 0.18 },
          },
        ],
      };
    });
    return opt;
  }

  /* ---- 哑铃图:reward ---- */
  function rewardOption(t, narrow) {
    var names = CONFIGS.map(function (c) { return cfgName(c, narrow); }).reverse();
    var arrows = [];
    var basePts = [];
    var candPts = [];
    var midPts = [];
    CONFIGS.forEach(function (c, i) {
      var y = CONFIGS.length - 1 - i;
      arrows.push({
        type: "line", data: [[c.base.reward, y], [c.cand.reward, y]],
        symbol: ["none", "arrow"], symbolSize: 12, silent: true,
        lineStyle: { color: TEAL, width: 3 }, z: 2, animation: !REDUCED,
      });
      basePts.push({ value: [c.base.reward, y], label: { position: "bottom", color: MUTED } });
      candPts.push({
        value: [c.cand.reward, y],
        label: { position: "top", color: TEAL, fontWeight: "bold" },
      });
      midPts.push({
        value: [(c.base.reward + c.cand.reward) / 2, y],
        label: {
          position: "bottom", distance: 14, color: RED, fontSize: 10.5,
          formatter: pp(c.cand.reward - c.base.reward),
        },
      });
    });
    var opt = baseGrid();
    opt.title = {
      text: t.rewardTitle, left: "center",
      textStyle: { color: INK, fontSize: narrow ? 12.5 : 15 },
    };
    opt.tooltip = {
      trigger: "item", confine: true,
      formatter: function (p) {
        var c = CONFIGS[CONFIGS.length - 1 - p.value[1]];
        return c.name + "<br/>" + t.base + ": " + c.base.reward.toFixed(4) +
          "<br/>" + t.cand + ": " + c.cand.reward.toFixed(4) +
          "<br/>" + t.gainLabel + ": " + pp(c.cand.reward - c.base.reward);
      },
    };
    opt.xAxis = Object.assign(axisCommon(), {
      type: "value", min: 0.3, max: 0.8, splitLine: { show: true, lineStyle: { color: LINE, opacity: 0.7 } },
    });
    // 为横轴刻度与底部图例分别预留空间，避免两者落在同一条布局带内。
    opt.grid = narrow
      ? { left: 12, right: 20, top: 44, bottom: 64, containLabel: true }
      : { left: 236, right: 20, top: 44, bottom: 64, containLabel: true };
    opt.yAxis = Object.assign(axisCommon(), {
      type: "category", data: names, splitLine: { show: false },
      axisLabel: { color: INK, fontSize: narrow ? 9.5 : 11 },
    });
    opt.legend = {
      bottom: 0, textStyle: { color: MUTED, fontSize: narrow ? 10 : 12 },
      data: [t.base, t.cand],
    };
    opt.series = arrows.concat([
      {
        name: t.base, type: "scatter", data: basePts, symbolSize: 14,
        itemStyle: { color: GRAY }, z: 3,
        label: { show: true, formatter: function (p) { return p.value[0].toFixed(3); }, fontSize: 10.5 },
      },
      {
        name: t.cand, type: "scatter", data: candPts, symbolSize: 16,
        itemStyle: { color: TEAL }, z: 3,
        label: { show: true, formatter: function (p) { return p.value[0].toFixed(4); }, fontSize: 11 },
      },
      { type: "scatter", data: midPts, symbolSize: 0, silent: true, label: { show: true }, z: 4 },
    ]);
    return opt;
  }

  /* ---- 分组柱状图(Pass^k 或 数据集+Reward) ---- */
  function barsOption(cfg, metrics, pickVals, t, withLegend, narrow) {
    var vb = pickVals(cfg.base);
    var vc = pickVals(cfg.cand);
    var opt = baseGrid();
    opt.title = {
      text: cfgName(cfg, narrow), left: "center",
      textStyle: { color: INK, fontSize: narrow ? 11.5 : 12.5 },
    };
    opt.grid = { left: 8, right: 8, top: 40, bottom: 4, containLabel: true };
    if (withLegend) {
      opt.legend = { top: 22, textStyle: { color: MUTED, fontSize: 10 }, itemWidth: 12, itemHeight: 9 };
      opt.grid.top = 58;
    }
    opt.tooltip = {
      trigger: "axis", confine: true,
      axisPointer: { type: "shadow" },
      formatter: function (ps) {
        var i = ps[0].dataIndex;
        return metrics[i] + "<br/>" + t.base + ": " + vb[i].toFixed(3) +
          "<br/>" + t.cand + ": " + vc[i].toFixed(3) +
          '<br/><span style="color:' + RED + '">' + pp(vc[i] - vb[i]) + "</span>";
      },
    };
    opt.xAxis = Object.assign(axisCommon(), {
      type: "category", data: metrics, splitLine: { show: false },
      axisLabel: { color: INK, fontSize: narrow ? 9 : 10 },
    });
    opt.yAxis = Object.assign(axisCommon(), { type: "value", max: 1.0 });
    opt.series = [
      { name: t.base, type: "bar", data: vb, barGap: "12%", itemStyle: { color: GRAY, borderRadius: [3, 3, 0, 0] } },
      {
        name: t.cand, type: "bar", data: vc,
        itemStyle: { color: BLUE, borderRadius: [3, 3, 0, 0] },
        label: {
          show: true, position: "top",
          formatter: function (p) {
            return "{v|" + vc[p.dataIndex].toFixed(3) + "}\n{p|" + pp(vc[p.dataIndex] - vb[p.dataIndex]) + "}";
          },
          rich: {
            v: { color: BLUE, fontWeight: "bold", fontSize: narrow ? 9 : 10.5, align: "center" },
            p: { color: RED, fontSize: narrow ? 8.5 : 9.5, align: "center" },
          },
        },
      },
    ];
    return opt;
  }

  /* ---- 提升热力图 ---- */
  function heatmapOption(t, narrow) {
    var data = [];
    var max = 0;
    CONFIGS.forEach(function (c, r) {
      for (var k = 0; k < 4; k++) {
        var g = (c.cand.pass[k] - c.base.pass[k]) * 100;
        max = Math.max(max, g);
        data.push([k, r, g]);
      }
    });
    var opt = baseGrid();
    opt.title = {
      text: t.heatTitle, left: "center",
      textStyle: { color: INK, fontSize: narrow ? 12 : 14 },
    };
    opt.grid = narrow
      ? { left: 12, right: 12, top: 46, bottom: 56, containLabel: true }
      : { left: 236, right: 12, top: 46, bottom: 56 };
    opt.tooltip = {
      confine: true,
      formatter: function (p) {
        var c = CONFIGS[p.value[1]];
        var k = p.value[0];
        return c.name + " · Pass^" + (k + 1) + "<br/>" +
          t.base + ": " + c.base.pass[k].toFixed(3) + " → " + t.cand + ": " + c.cand.pass[k].toFixed(3) +
          '<br/><span style="color:' + RED + '">' + pp(c.cand.pass[k] - c.base.pass[k]) + "</span>";
      },
    };
    opt.xAxis = Object.assign(axisCommon(), {
      type: "category", data: ["Pass^1", "Pass^2", "Pass^3", "Pass^4"],
      splitLine: { show: false }, axisLabel: { color: INK, fontSize: narrow ? 9.5 : 11 },
    });
    opt.yAxis = Object.assign(axisCommon(), {
      type: "category",
      data: CONFIGS.map(function (c) { return cfgName(c, narrow); }),
      splitLine: { show: false }, axisLabel: { color: INK, fontSize: narrow ? 9 : 10.5 },
    });
    opt.visualMap = {
      min: 0, max: Math.ceil(max), calculable: false, orient: "horizontal",
      left: "center", bottom: 0, text: [t.heatHigh, t.heatLow],
      textStyle: { color: MUTED, fontSize: 10 },
      inRange: { color: ["#eef7ee", "#a8d5ba", "#5aa9e6", "#1d6fc2"] },
    };
    opt.series = [{
      type: "heatmap", data: data,
      label: {
        show: true, fontWeight: "bold", fontSize: narrow ? 10 : 11.5,
        formatter: function (p) { return "+" + p.value[2].toFixed(1); },
      },
      itemStyle: { borderColor: "#fff", borderWidth: 3, borderRadius: 4 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(22,35,58,0.25)" } },
    }];
    return opt;
  }

  /* ---- Pass^k 曲线小多图 ---- */
  function curveOption(cfg, t, withLegend, narrow) {
    var opt = baseGrid();
    opt.title = {
      text: cfgName(cfg, narrow), left: "center",
      textStyle: { color: INK, fontSize: narrow ? 11.5 : 12.5 },
    };
    opt.grid = { left: 8, right: 14, top: 40, bottom: 26, containLabel: true };
    if (withLegend) {
      opt.legend = { top: 22, textStyle: { color: MUTED, fontSize: 10 }, itemWidth: 14, itemHeight: 9,
        data: [t.base, t.cand] };
      opt.grid.top = 58;
    }
    opt.tooltip = {
      trigger: "axis", confine: true,
      formatter: function (ps) {
        var i = ps[0].dataIndex;
        return "Pass^" + (i + 1) + "<br/>" + t.base + ": " + cfg.base.pass[i].toFixed(3) +
          "<br/>" + t.cand + ": " + cfg.cand.pass[i].toFixed(3) +
          '<br/><span style="color:' + RED + '">' + pp(cfg.cand.pass[i] - cfg.base.pass[i]) + "</span>";
      },
    };
    opt.xAxis = Object.assign(axisCommon(), {
      type: "category", data: ["Pass^1", "Pass^2", "Pass^3", "Pass^4"],
      boundaryGap: false, splitLine: { show: false },
      axisLabel: { color: INK, fontSize: narrow ? 9 : 10 },
      name: t.kAxis, nameLocation: "middle", nameGap: 24,
      nameTextStyle: { color: MUTED, fontSize: 9.5 },
    });
    opt.yAxis = Object.assign(axisCommon(), { type: "value", min: 0.1, max: 0.78 });
    opt.series = [
      {
        name: t.base, type: "line", data: cfg.base.pass, smooth: false,
        symbol: "circle", symbolSize: 7, lineStyle: { color: GRAY, width: 2.2 },
        itemStyle: { color: GRAY }, z: 2,
      },
      {
        name: "_gap", type: "line", data: cfg.base.pass, stack: "gap",
        symbol: "none", lineStyle: { opacity: 0 }, silent: true, z: 1,
      },
      {
        name: "_gapFill", type: "line", stack: "gap",
        data: cfg.cand.pass.map(function (v, i) { return v - cfg.base.pass[i]; }),
        symbol: "none", lineStyle: { opacity: 0 }, silent: true,
        areaStyle: { color: BLUE, opacity: 0.10 }, z: 1,
      },
      {
        name: t.cand, type: "line", data: cfg.cand.pass, smooth: false,
        symbol: "circle", symbolSize: 7, lineStyle: { color: BLUE, width: 2.4 },
        itemStyle: { color: BLUE }, z: 3,
        label: {
          show: true, position: "top", fontSize: narrow ? 8.5 : 9.5, color: RED,
          formatter: function (p) { return pp(cfg.cand.pass[p.dataIndex] - cfg.base.pass[p.dataIndex]); },
        },
      },
    ];
    return opt;
  }

  /* ---- 进化轨迹 ---- */
  function trendOption(t, narrow) {
    var best = [];
    var cur = 0;
    TREND.train.forEach(function (v) { cur = Math.max(cur, v); best.push(cur); });
    var cats = TREND.stamps.map(function (s, i) { return "#" + (i + 1) + "\n" + s; });
    var opt = baseGrid();
    opt.title = {
      text: t.trendTitle, left: "center",
      textStyle: { color: INK, fontSize: narrow ? 12 : 14 },
    };
    opt.grid = narrow
      ? { left: 16, right: 24, top: 46, bottom: 34, containLabel: true }
      : { left: 16, right: 24, top: 66, bottom: 8, containLabel: true };
    opt.legend = narrow
      ? { bottom: 0, textStyle: { color: MUTED, fontSize: 9 } }
      : { top: 34, textStyle: { color: MUTED, fontSize: 10.5 } };
    opt.tooltip = { trigger: "axis", confine: true, valueFormatter: function (v) { return v == null ? "-" : Number(v).toFixed(3); } };
    opt.xAxis = Object.assign(axisCommon(), {
      type: "category", data: cats, boundaryGap: false, splitLine: { show: false },
      axisLabel: {
        color: MUTED, fontSize: narrow ? 8 : 9, lineHeight: 12,
        interval: narrow ? 2 : 0,
      },
    });
    opt.yAxis = Object.assign(axisCommon(), {
      type: "value", min: 0, max: 0.85, name: "total_score",
      nameTextStyle: { color: MUTED, fontSize: 10 },
    });
    opt.series = [
      {
        name: narrow ? t.trendTrainS : t.trendTrain, type: "line", data: TREND.train,
        symbol: "circle", symbolSize: 8, lineStyle: { color: BLUE, width: 2.4 },
        itemStyle: { color: BLUE },
        label: { show: !narrow, position: "top", fontSize: 9, color: BLUE, formatter: function (p) { return p.value.toFixed(3); } },
      },
      {
        name: narrow ? t.trendValS : t.trendVal, type: "line", data: TREND.val, connectNulls: false,
        symbol: "rect", symbolSize: 8, lineStyle: { color: RED, width: 2.2 },
        itemStyle: { color: RED },
        label: { show: !narrow, position: "bottom", fontSize: 9, color: RED, formatter: function (p) { return p.value == null ? "" : p.value.toFixed(3); } },
      },
      {
        name: narrow ? t.trendBestS : t.trendBest, type: "line", data: best, step: "end",
        symbol: "none", lineStyle: { color: BLUE, width: 1.2, type: "dashed", opacity: 0.5 },
        itemStyle: { color: BLUE },
      },
    ];
    return opt;
  }

  /* ---- 装配与渲染 ---- */
  function passVals(r) { return r.pass; }
  function splitVals(r) { return [r.train, r.val, r.test, r.reward]; }

  function buildAll(lang, narrow) {
    var t = text(lang);
    var out = {};
    out["chart-benchmark-overview"] = benchmarkOverviewOption(t, narrow);
    out["chart-reward"] = rewardOption(t, narrow);
    out["chart-heatmap"] = heatmapOption(t, narrow);
    out["chart-trend"] = trendOption(t, narrow);
    CONFIGS.forEach(function (cfg, i) {
      out["chart-pass-" + i] = barsOption(cfg, ["Pass^1", "Pass^2", "Pass^3", "Pass^4"], passVals, t, i === 0, narrow);
      out["chart-splits-" + i] = barsOption(cfg, t.splits, splitVals, t, i === 0, narrow);
      out["chart-curve-" + i] = curveOption(cfg, t, i === 0, narrow);
    });
    EVO_CONFIGS.forEach(function (cfg, i) {
      var namedCfg = { name: t[cfg.nameKey], base: cfg.base, cand: cfg.cand };
      out["chart-evo-" + i] = barsOption(namedCfg, t.splits, splitVals, t, i === 0, narrow);
    });
    return out;
  }

  var inited = {};   // id -> echarts instance
  var currentLang = "zh";
  var currentNarrow = isNarrow();

  function isVisible(el) {
    // 折叠在 <details> 内的容器没有布局尺寸,此时交给 IntersectionObserver
    return el.clientWidth > 0 && el.clientHeight > 0;
  }

  function renderId(id, option) {
    var el = document.getElementById(id);
    if (!el || !window.echarts) return;
    if (inited[id]) {
      inited[id].setOption(option, { notMerge: true });
    } else if (isVisible(el)) {
      inited[id] = window.echarts.init(el, null, { renderer: "canvas" });
      inited[id].setOption(option);
    }
  }

  function renderAll(lang) {
    currentLang = lang === "en" ? "en" : "zh";
    currentNarrow = isNarrow();
    var options = buildAll(currentLang, currentNarrow);
    Object.keys(options).forEach(function (id) { renderId(id, options[id]); });
  }

  function init() {
    if (!window.echarts) return;
    var lang = document.body.classList.contains("lang-en") ? "en" : "zh";
    currentLang = lang;
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            renderId(e.target.id, buildAll(currentLang, isNarrow())[e.target.id]);
            io.unobserve(e.target);
          }
        });
      }, { rootMargin: "120px" });
      Object.keys(buildAll(lang, currentNarrow)).forEach(function (id) {
        var el = document.getElementById(id);
        if (el) io.observe(el);
      });
    } else {
      renderAll(lang);
    }
    window.addEventListener("resize", function () {
      // 跨过窄屏断点时需要用窄屏配置整体重渲染
      if (isNarrow() !== currentNarrow) {
        renderAll(currentLang);
        return;
      }
      Object.keys(inited).forEach(function (id) {
        if (isVisible(document.getElementById(id))) inited[id].resize();
      });
    });
    // <details> 展开/收起时,修正已初始化图表的尺寸
    document.addEventListener("toggle", function (e) {
      if (e.target.tagName !== "DETAILS") return;
      Object.keys(inited).forEach(function (id) {
        var el = document.getElementById(id);
        if (el && e.target.contains(el) && isVisible(el)) inited[id].resize();
      });
    }, true);
  }

  window.LongarcCharts = {
    setLang: function (lang) {
      // 页面加载早期语言设置先于 IO 初始化,此时由 init() 读取 body class 即可
      if (Object.keys(inited).length === 0) return;
      renderAll(lang);
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
