<script setup lang="ts">
/** 项目与环境管理页：管理项目导入、源码文档工作区、环境配置及版本历史。认证保存环境变量引用，接口文档由独立组件追踪后台进度。 */

import { computed, onMounted, reactive, ref } from 'vue'
import {
  ChevronRight,
  FolderKanban,
  FolderPlus,
  FolderInput,
  History,
  Loader2,
  Plus,
  Save,
  ShieldCheck,
} from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import AppModal from '@/components/AppModal.vue'
import ProjectDocuments from '@/components/ProjectDocuments.vue'
import { describeError } from '@/api/client'
import { importProject } from '@/api/projectDocuments'
import {
  createEnvironment,
  createProject,
  environmentHistory,
  listEnvironments,
  listProjects,
  updateEnvironment,
} from '@/api/endpoints'
import type { Environment, EnvironmentHistory, Project } from '@/api/types'
import { formatDateTime } from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']

const projects = ref<Project[]>([])
const environments = ref<Environment[]>([])
const selectedProject = ref<Project | null>(null)
const selectedEnvironment = ref<Environment | null>(null)
const history = ref<EnvironmentHistory | null>(null)
const loading = ref(false)

const projectForm = reactive({ name: '', description: '' })
const creatingProject = ref(false)
const showProjectForm = ref(false)
const showImportForm = ref(false)
const importForm = reactive({ name: '', description: '', directory: '' })
const importingProject = ref(false)
const importError = ref('')
const activeDetail = ref<'documents' | 'environment'>('documents')
let projectSelection = 0

/** 环境表单：origin 与方法/路径用文本编辑，避免多值输入控件的复杂度。 */
const environmentForm = reactive({
  name: '',
  origin: 'http://127.0.0.1:8080',
  methods: 'GET,POST' as string,
  paths: '/health' as string,
  authKind: 'none' as 'none' | 'bearer' | 'api_key' | 'header',
  authEnvVar: '',
  authHeaderName: '',
  sensitive: '' as string,
})
const creatingEnvironment = ref(false)
const savingEnvironment = ref(false)
const showEnvironmentForm = ref(false)

/** 将逗号分隔方法转换为去空白的大写列表；只做输入归一化，合法性由后端校验。 */
const parsedMethods = computed(() =>
  environmentForm.methods
    .split(',')
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean),
)

/** 将多行路径转换为去空白的非空列表；不扩展模板或验证授权范围。 */
const parsedPaths = computed(() =>
  environmentForm.paths
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean),
)

/** 切换当前项目并清空旧环境选中及历史，再读取该项目环境；失败提示并清空环境列表，始终解除 loading。 */
async function selectProject(project: Project) {
  const selection = ++projectSelection
  selectedProject.value = project
  selectedEnvironment.value = null
  history.value = null
  environments.value = []
  showEnvironmentForm.value = false
  activeDetail.value = 'documents'
  loading.value = true
  try {
    const page = await listEnvironments(project.id)
    if (selection !== projectSelection) return
    environments.value = page.items
  } catch (error) {
    if (selection !== projectSelection) return
    useToastStore().push('danger', '加载环境失败', describeError(error))
    environments.value = []
  } finally {
    if (selection === projectSelection) loading.value = false
  }
}

/** 切换当前环境并读取版本历史；失败时清空历史并提示，不修改已保存版本。 */
async function selectEnvironment(environment: Environment) {
  selectedEnvironment.value = environment
  activeDetail.value = 'environment'
  showEnvironmentForm.value = false
  try {
    const result = await environmentHistory(environment.id)
    if (selectedEnvironment.value?.id === environment.id) history.value = result
  } catch (error) {
    if (selectedEnvironment.value?.id !== environment.id) return
    useToastStore().push('danger', '加载版本历史失败', describeError(error))
    history.value = null
  }
}

/** 创建项目并导入本机源码目录，成功后直接进入文档工作区。 */
async function submitImport(): Promise<void> {
  if (!importForm.name.trim() || !importForm.directory.trim() || importingProject.value) return
  importingProject.value = true
  importError.value = ''
  try {
    const result = await importProject({
      name: importForm.name.trim(), description: importForm.description.trim(), directory: importForm.directory.trim(),
    })
    projects.value.push(result.project)
    showImportForm.value = false
    importForm.name = ''
    importForm.description = ''
    importForm.directory = ''
    await selectProject(result.project)
    useToastStore().push(result.source.complete ? 'success' : 'warning',
      result.source.complete ? '项目已导入，可生成接口文档' : '项目已导入，请检查源码扫描问题')
  } catch (failure) {
    importError.value = describeError(failure)
  } finally {
    importingProject.value = false
  }
}

/** 新建环境从空身份开始，避免已有环境的选中态把创建入口变成版本编辑。 */
function startEnvironment(): void {
  selectedEnvironment.value = null
  history.value = null
  activeDetail.value = 'environment'
  showEnvironmentForm.value = true
  Object.assign(environmentForm, { name: '', origin: 'http://127.0.0.1:8080', methods: 'GET,POST',
    paths: '/health', authKind: 'none', authEnvVar: '', authHeaderName: '', sensitive: '' })
}

/** 读取项目列表并默认加载第一项的环境；失败提示且始终解除 loading。 */
async function loadProjects() {
  loading.value = true
  try {
    const page = await listProjects(100)
    projects.value = page.items
    if (page.items.length) await selectProject(page.items[0])
  } catch (error) {
    useToastStore().push('danger', '加载项目失败', describeError(error))
  } finally {
    loading.value = false
  }
}

/** 校验名称后创建项目，追加本地列表并选中；失败提示，始终解除创建态。 */
async function submitProject() {
  if (!projectForm.name.trim()) {
    useToastStore().push('warning', '请填写项目名称')
    return
  }
  creatingProject.value = true
  try {
    const project = await createProject({
      name: projectForm.name.trim(),
      description: projectForm.description.trim(),
    })
    projects.value.push(project)
    projectForm.name = ''
    projectForm.description = ''
    showProjectForm.value = false
    await selectProject(project)
    useToastStore().push('success', '项目已创建')
  } catch (error) {
    useToastStore().push('danger', '创建项目失败', describeError(error))
  } finally {
    creatingProject.value = false
  }
}

/** 把环境表单转换为后端输入，解析方法、路径及敏感字段规则；认证仅引用环境变量，默认关闭重放，不验证服务端范围策略。 */
function buildEnvironmentInput() {
  return {
    name: environmentForm.name.trim(),
    origin: environmentForm.origin.trim(),
    allowed_methods: parsedMethods.value,
    path_templates: parsedPaths.value,
    auth_ref:
      environmentForm.authKind === 'none'
        ? null
        : {
            kind: environmentForm.authKind as 'bearer' | 'api_key' | 'header',
            env_var: environmentForm.authEnvVar.trim(),
            header_name:
              environmentForm.authKind === 'bearer' ? null : environmentForm.authHeaderName.trim(),
          },
    replay_policy: { allow_replay: false, allow_non_idempotent: false },
    sensitive_field_rules: environmentForm.sensitive
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean),
  }
}

/** 在已选项目下创建具名环境，成功追加并展示版本历史；缺少名称时拦截，接口失败提示并解除创建态。 */
async function submitEnvironment() {
  if (!selectedProject.value) return
  const payload = buildEnvironmentInput()
  if (!payload.name) {
    useToastStore().push('warning', '请填写环境名称')
    return
  }
  creatingEnvironment.value = true
  try {
    const created = await createEnvironment(selectedProject.value.id, payload)
    environments.value.push(created)
    environmentForm.name = ''
    showEnvironmentForm.value = false
    await selectEnvironment(created)
    useToastStore().push('success', '环境已创建（版本 1）')
  } catch (error) {
    useToastStore().push('danger', '创建环境失败', describeError(error))
  } finally {
    creatingEnvironment.value = false
  }
}

/** 以当前环境版本号作为乐观锁追加配置版本；空 origin、方法、路径沿用旧配置，成功同步列表及历史，冲突或其他错误提示。 */
async function saveVersion() {
  if (!selectedEnvironment.value) return
  const payload = buildEnvironmentInput()
  savingEnvironment.value = true
  try {
    // 乐观锁：必须带上当前版本号，后端会拒绝过期写入。
    const updated = await updateEnvironment(selectedEnvironment.value.id, {
      origin: payload.origin || selectedEnvironment.value.config.origin,
      allowed_methods: payload.allowed_methods.length
        ? payload.allowed_methods
        : selectedEnvironment.value.config.allowed_methods,
      path_templates: payload.path_templates.length
        ? payload.path_templates
        : selectedEnvironment.value.config.path_templates,
      auth_ref: payload.auth_ref,
      replay_policy: payload.replay_policy,
      sensitive_field_rules: payload.sensitive_field_rules,
      expected_version: selectedEnvironment.value.current_version,
    })
    selectedEnvironment.value = updated
    const index = environments.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) environments.value[index] = updated
    history.value = await environmentHistory(updated.id)
    useToastStore().push('success', `已追加版本 ${updated.current_version}`)
  } catch (error) {
    useToastStore().push('danger', '保存失败', describeError(error))
  } finally {
    savingEnvironment.value = false
  }
}

/** 将指定环境的当前配置填回编辑表单；只恢复认证引用和规则，不读取认证值或立即保存。 */
function startEdit(environment: Environment) {
  showEnvironmentForm.value = true
  environmentForm.name = environment.name
  environmentForm.origin = environment.config.origin
  environmentForm.methods = environment.config.allowed_methods.join(',')
  environmentForm.paths = environment.config.path_templates.join('\n')
  environmentForm.authKind = environment.config.auth_ref?.kind ?? 'none'
  environmentForm.authEnvVar = environment.config.auth_ref?.env_var ?? ''
  environmentForm.authHeaderName = environment.config.auth_ref?.header_name ?? ''
  environmentForm.sensitive = environment.config.sensitive_field_rules.join(',')
}

onMounted(loadProjects)
</script>

<template>
  <div class="projects">
    <!-- 项目列 -->
    <aside class="col projects-col">
      <div class="col-head">
        <span class="col-title">项目</span>
        <div class="card-actions-inline">
          <button class="btn btn-ghost btn-sm" title="导入项目" @click="showImportForm = true">
            <FolderInput :size="13" />导入
          </button>
          <button class="btn btn-ghost btn-sm" title="新建项目" @click="showProjectForm = !showProjectForm">
            <FolderPlus :size="13" />
          </button>
        </div>
      </div>

      <div v-if="showProjectForm" class="inline-form">
        <input v-model="projectForm.name" class="input" placeholder="项目名称" maxlength="80" />
        <input v-model="projectForm.description" class="input" placeholder="描述（可选）" maxlength="500" />
        <button class="btn btn-primary btn-sm" :disabled="creatingProject" @click="submitProject">
          <Loader2 v-if="creatingProject" :size="12" class="spinning" />
          <Plus v-else :size="12" />
          创建
        </button>
      </div>

      <div class="col-body scroll-y">
        <button
          v-for="project in projects"
          :key="project.id"
          class="list-item"
          :class="{ 'list-item-active': selectedProject?.id === project.id }"
          @click="selectProject(project)"
        >
          <div class="list-item-main">
            <span class="list-item-title truncate">{{ project.name }}</span>
            <span class="list-item-sub truncate">{{ project.description || '无描述' }}</span>
          </div>
          <ChevronRight :size="13" class="list-chevron" />
        </button>
        <EmptyState
          v-if="!projects.length && !loading"
          :icon="FolderKanban"
          title="还没有项目"
          description="导入项目源码可生成接口文档，也可新建项目管理环境。"
        />
      </div>
    </aside>

    <!-- 环境列 -->
    <aside class="col env-col">
      <div class="col-head">
        <span class="col-title">环境</span>
        <button
          class="btn btn-ghost btn-sm"
          :disabled="!selectedProject"
          title="新建环境"
          @click="startEnvironment"
        >
          <Plus :size="13" />
        </button>
      </div>
      <div class="col-body scroll-y">
        <button
          v-for="environment in environments"
          :key="environment.id"
          class="list-item"
          :class="{ 'list-item-active': selectedEnvironment?.id === environment.id }"
          @click="selectEnvironment(environment)"
        >
          <div class="list-item-main">
            <span class="list-item-title truncate">{{ environment.name }}</span>
            <span class="list-item-sub truncate">
              v{{ environment.current_version }} · {{ environment.config.origin }}
            </span>
          </div>
        </button>
        <EmptyState
          v-if="selectedProject && !environments.length && !loading"
          title="还没有环境"
          description="环境定义了允许访问的 origin、方法与路径模板。"
        />
      </div>
    </aside>

    <!-- 详情 -->
    <section class="detail scroll-y">
      <div v-if="selectedProject" class="detail-tabs" role="tablist" aria-label="项目内容">
        <button class="detail-tab" :class="{ active: activeDetail === 'documents' }" role="tab" :aria-selected="activeDetail === 'documents'" @click="activeDetail = 'documents'">接口文档</button>
        <button class="detail-tab" :class="{ active: activeDetail === 'environment' }" role="tab" :aria-selected="activeDetail === 'environment'" @click="activeDetail = 'environment'">环境配置</button>
      </div>
      <ProjectDocuments v-if="selectedProject" v-show="activeDetail === 'documents'" :key="selectedProject.id" :project-id="selectedProject.id" />
      <template v-if="activeDetail === 'environment'">
      <div v-if="showEnvironmentForm" class="detail-card">
        <div class="card-head">
          <span class="card-title">{{
            selectedEnvironment ? `编辑「${selectedEnvironment.name}」` : '新建环境'
          }}</span>
          <button class="btn btn-ghost btn-sm" @click="showEnvironmentForm = false">取消</button>
        </div>
        <div class="form-grid">
          <label v-if="!selectedEnvironment" class="field">
            <span class="field-label">环境名称</span>
            <input v-model="environmentForm.name" class="input" placeholder="dev / test / staging" maxlength="80" />
          </label>
          <label class="field">
            <span class="field-label">Origin</span>
            <input v-model="environmentForm.origin" class="input mono" placeholder="http://127.0.0.1:8080" />
            <span class="hint">不带路径与查询串，例如 http://127.0.0.1:8080</span>
          </label>
          <label class="field">
            <span class="field-label">允许方法（逗号分隔）</span>
            <input v-model="environmentForm.methods" class="input mono" :placeholder="METHODS.join(',')" />
          </label>
          <label class="field field-wide">
            <span class="field-label">路径模板（每行一个，可用 {id} 占位）</span>
            <textarea v-model="environmentForm.paths" class="textarea mono" rows="4" />
          </label>
          <label class="field">
            <span class="field-label">鉴权方式</span>
            <select v-model="environmentForm.authKind" class="select">
              <option value="none">不使用</option>
              <option value="bearer">Bearer Token</option>
              <option value="api_key">API Key 头</option>
              <option value="header">自定义头</option>
            </select>
          </label>
          <label v-if="environmentForm.authKind !== 'none'" class="field">
            <span class="field-label">环境变量名</span>
            <input v-model="environmentForm.authEnvVar" class="input mono" placeholder="CRM_TEST_TOKEN" />
            <span class="hint">只存变量名；值必须留在本机进程环境里</span>
          </label>
          <label
            v-if="environmentForm.authKind === 'api_key' || environmentForm.authKind === 'header'"
            class="field"
          >
            <span class="field-label">头名称</span>
            <input v-model="environmentForm.authHeaderName" class="input mono" placeholder="X-Api-Key" />
          </label>
          <label class="field field-wide">
            <span class="field-label">敏感字段（JSON Pointer，逗号分隔）</span>
            <input v-model="environmentForm.sensitive" class="input mono" placeholder="/data/password, /token" />
          </label>
        </div>
        <div class="card-actions">
          <button
            v-if="!selectedEnvironment"
            class="btn btn-primary"
            :disabled="creatingEnvironment"
            @click="submitEnvironment"
          >
            <Loader2 v-if="creatingEnvironment" :size="13" class="spinning" />
            <Plus v-else :size="13" />
            创建环境
          </button>
          <button v-else class="btn btn-primary" :disabled="savingEnvironment" @click="saveVersion">
            <Loader2 v-if="savingEnvironment" :size="13" class="spinning" />
            <Save v-else :size="13" />
            保存为新版本
          </button>
        </div>
      </div>

      <div v-else-if="selectedEnvironment" class="detail-stack">
        <div class="detail-card">
          <div class="card-head">
            <span class="card-title">{{ selectedEnvironment.name }}</span>
            <div class="card-actions-inline">
              <span class="tag tag-accent">v{{ selectedEnvironment.current_version }}</span>
              <button class="btn btn-sm" @click="startEdit(selectedEnvironment)">编辑配置</button>
            </div>
          </div>
          <dl class="kv">
            <dt>Origin</dt>
            <dd class="mono">{{ selectedEnvironment.config.origin }}</dd>
            <dt>允许方法</dt>
            <dd>
              <span v-for="method in selectedEnvironment.config.allowed_methods" :key="method" class="tag mono">
                {{ method }}
              </span>
            </dd>
            <dt>路径模板</dt>
            <dd class="mono paths">
              <div v-for="path in selectedEnvironment.config.path_templates" :key="path">{{ path }}</div>
            </dd>
            <dt>鉴权引用</dt>
            <dd>
              <template v-if="selectedEnvironment.config.auth_ref">
                <span class="tag tag-info">{{ selectedEnvironment.config.auth_ref.kind }}</span>
                <code>{{ selectedEnvironment.config.auth_ref.env_var }}</code>
                <span v-if="selectedEnvironment.config.auth_ref.header_name" class="hint">
                  → {{ selectedEnvironment.config.auth_ref.header_name }}
                </span>
              </template>
              <span v-else class="hint">未配置（计划不能引用凭据）</span>
            </dd>
            <dt>重放策略</dt>
            <dd>
              <span class="tag">
                {{ selectedEnvironment.config.replay_policy.allow_replay ? '允许重放' : '禁止重放' }}
              </span>
              <span v-if="selectedEnvironment.config.replay_policy.allow_non_idempotent" class="tag tag-warning">
                允许非幂等
              </span>
            </dd>
            <dt>敏感字段</dt>
            <dd>
              <span
                v-for="rule in selectedEnvironment.config.sensitive_field_rules"
                :key="rule"
                class="tag mono"
              >
                {{ rule }}
              </span>
              <span v-if="!selectedEnvironment.config.sensitive_field_rules.length" class="hint">
                未配置
              </span>
            </dd>
          </dl>
        </div>

        <div v-if="history" class="detail-card">
          <div class="card-head">
            <span class="card-title">
              <History :size="13" style="vertical-align: -2px" />
              版本历史
            </span>
            <span class="hint">每次保存都是追加，不覆盖旧版本</span>
          </div>
          <table class="table">
            <thead>
              <tr>
                <th>版本</th>
                <th>Origin</th>
                <th>方法</th>
                <th>路径数</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="version in [...history.versions].reverse()" :key="version.id">
                <td>
                  <span class="tag" :class="{ 'tag-accent': version.version === history.current_version }">
                    v{{ version.version }}
                  </span>
                </td>
                <td class="mono">{{ version.origin }}</td>
                <td class="mono">{{ version.allowed_methods.join(', ') }}</td>
                <td>{{ version.path_templates.length }}</td>
                <td class="hint">{{ formatDateTime(version.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="detail-card subtle-card">
          <div class="card-head">
            <span class="card-title"><ShieldCheck :size="13" style="vertical-align: -2px" /> 安全边界</span>
          </div>
          <p class="note">
            环境只保存 origin 白名单与<strong>凭据引用</strong>，绝不保存密钥本身。
            真正执行时，后端从本机进程环境按变量名读取值，并只允许
            <code>TARGET_AUTH_ENV_VARS</code> 里列出的变量。
          </p>
        </div>
      </div>

      <EmptyState
        v-else
        :icon="FolderKanban"
        title="选择左侧环境或新建环境"
        description="这里会显示 origin 白名单、路径模板、鉴权引用与完整版本历史。"
      />
      </template>
      <EmptyState v-if="!selectedProject" :icon="FolderKanban" title="导入或选择项目" description="导入项目源码后可生成接口文档并导出，也可为项目添加测试环境。" />
    </section>
    <AppModal v-model:open="showImportForm" title="导入项目" width="580px" :confirm-text="importingProject ? '扫描项目中' : '导入并扫描'" :confirm-disabled="importingProject || !importForm.name.trim() || !importForm.directory.trim()" @confirm="submitImport">
      <div class="import-fields">
        <label class="field"><span class="field-label">项目名称</span><input v-model="importForm.name" class="input" maxlength="80" placeholder="填写项目名称" :disabled="importingProject" /></label>
        <label class="field"><span class="field-label">项目目录</span><input v-model="importForm.directory" class="input mono" placeholder="本机项目的绝对路径" :disabled="importingProject" /></label>
        <p class="hint">填写运行 API-Agent 电脑上的目录，导入会递归扫描源码并保存快照。</p>
        <label class="field"><span class="field-label">项目描述（可选）</span><textarea v-model="importForm.description" class="textarea" rows="2" maxlength="500" :disabled="importingProject" /></label>
        <p v-if="importError" class="import-error" role="alert">{{ importError }}</p>
      </div>
    </AppModal>
  </div>
</template>

<style scoped>
.projects {
  display: flex;
  height: 100%;
  min-height: 0;
}

.col {
  display: flex;
  flex-direction: column;
  flex: none;
  min-height: 0;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}

.projects-col {
  width: 236px;
}

.env-col {
  width: 232px;
  background: var(--bg-canvas);
}

.col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 38px;
  padding: 0 8px 0 13px;
  flex: none;
  color: var(--text-secondary);
}

.col-title {
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.col-body {
  flex: 1;
  padding: 0 7px 12px;
  min-height: 0;
}

.inline-form {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 0 10px 9px;
  flex: none;
}

.inline-form .input {
  height: 29px;
}

.list-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
}

.list-item:hover {
  background: var(--bg-hover);
}

.list-item-active {
  background: var(--nav-active-gradient);
  border-color: var(--accent-border);
}

.list-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.list-item-title {
  font-size: 12.5px;
  font-weight: 550;
}

.list-item-sub {
  font-size: 10.5px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.list-chevron {
  color: var(--text-tertiary);
  flex: none;
}

.detail {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 16px;
}

.detail-stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-tabs { display: flex; gap: 18px; margin: -4px 0 16px; border-bottom: 1px solid var(--border-subtle); }
.detail-tab { padding: 10px 0; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-secondary); font-size: 12px; }
.detail-tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.import-fields { display: flex; flex-direction: column; gap: 12px; }
.import-fields .hint { margin: -5px 0 0; }
.import-error { margin: 0; color: var(--danger); font-size: 12px; }

.detail-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface-gradient);
  box-shadow: var(--shadow-sm);
  padding: 13px 15px 15px;
}

.subtle-card {
  background: var(--bg-subtle);
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 11px;
}

.card-title {
  font-size: 13px;
  font-weight: 650;
}

.card-actions-inline {
  display: flex;
  align-items: center;
  gap: 7px;
}

.card-actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 11px;
}

.field-wide {
  grid-column: 1 / -1;
}

.kv {
  display: grid;
  grid-template-columns: 104px 1fr;
  gap: 8px 14px;
  margin: 0;
  font-size: 12px;
}

.kv dt {
  color: var(--text-tertiary);
  font-size: 11.5px;
}

.kv dd {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  word-break: break-word;
}

.paths {
  flex-direction: column;
  align-items: flex-start !important;
  gap: 1px !important;
}

.note {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--text-secondary);
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1080px) {
  /* 窄窗口纵向排列项目、环境和详情，让导入入口与表单保持可滚动、可操作。 */
  .projects {
    flex-direction: column;
    height: auto;
    min-height: 100%;
    min-width: 300px;
  }

  .projects-col,
  .env-col {
    width: 100%;
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .col-body {
    max-height: 160px;
  }

  .detail {
    overflow: visible;
    padding: 12px;
  }

  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
