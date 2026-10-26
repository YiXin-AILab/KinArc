import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
  type RouterHistory,
} from 'vue-router'

import ChatPage from './pages/ChatPage.vue'
import AgentEditPage from './platform/AgentEditPage.vue'
import AgentsListPage from './platform/AgentsListPage.vue'
import AgentWizardPage from './platform/AgentWizardPage.vue'
import EvaluationDetailPage from './platform/EvaluationDetailPage.vue'
import EvaluationListPage from './platform/EvaluationListPage.vue'
import EvolutionCreatePage from './platform/EvolutionCreatePage.vue'
import EvolutionDetailPage from './platform/EvolutionDetailPage.vue'
import EvolutionListPage from './platform/EvolutionListPage.vue'
import KbGeneratePage from './platform/KbGeneratePage.vue'
import KbListPage from './platform/KbListPage.vue'
import KbTestPage from './platform/KbTestPage.vue'
import KbWorkspacePage from './platform/KbWorkspacePage.vue'
import SkillEditPage from './platform/SkillEditPage.vue'
import SkillRepoPage from './platform/SkillRepoPage.vue'

export const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', name: 'chat', component: ChatPage },
  {
    path: '/platform/agents',
    name: 'platform-agents',
    component: AgentsListPage,
  },
  {
    path: '/platform/agents/new',
    name: 'platform-agent-new',
    component: AgentWizardPage,
  },
  {
    path: '/platform/agents/:id/edit',
    name: 'platform-agent-edit',
    component: AgentEditPage,
  },
  {
    path: '/platform/skills',
    name: 'platform-skills',
    component: SkillRepoPage,
  },
  {
    path: '/platform/skills/new',
    name: 'platform-skill-new',
    component: SkillEditPage,
  },
  {
    path: '/platform/skills/:scope/:name/edit',
    name: 'platform-skill-edit',
    component: SkillEditPage,
  },
  {
    path: '/platform/knowledge',
    name: 'platform-kb-list',
    component: KbListPage,
  },
  {
    path: '/platform/knowledge/generate',
    name: 'platform-kb-generate',
    component: KbGeneratePage,
  },
  {
    path: '/platform/knowledge/:scope/:name',
    name: 'platform-kb-workspace',
    component: KbWorkspacePage,
  },
  {
    path: '/platform/knowledge/:scope/:name/test',
    name: 'platform-kb-test',
    component: KbTestPage,
  },
  {
    path: '/platform/evolution',
    name: 'evolution-list',
    component: EvolutionListPage,
  },
  {
    path: '/platform/evolution/new',
    name: 'evolution-new',
    component: EvolutionCreatePage,
  },
  {
    path: '/platform/evolution/:id',
    name: 'evolution-detail',
    component: EvolutionDetailPage,
  },
  {
    path: '/platform/evaluation',
    name: 'evaluation-list',
    component: EvaluationListPage,
  },
  {
    path: '/platform/evaluation/runs/:runId',
    name: 'evaluation-detail',
    component: EvaluationDetailPage,
  },
]

export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    history: history ?? createWebHistory(),
    routes,
  })
}
