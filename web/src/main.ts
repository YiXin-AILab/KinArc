/**
 * 浏览器入口：加载全局样式并挂载根 Vue 应用。
 */

import { createApp } from 'vue'

import App from './App.vue'
import { createAppRouter } from './router'
import './styles.css'
import './platform/platform.css'

createApp(App).use(createAppRouter()).mount('#app')
