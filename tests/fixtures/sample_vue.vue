<template>
  <div class="app">
    <h1>{{ title }}</h1>
    <UserList :users="users" @select="onSelectUser" />
    <button @click="increment">Count: {{ count }}</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import UserList from './UserList.vue'

interface User {
  id: number
  name: string
}

const count = ref(0)
const title = ref('My App')
const users = ref<User[]>([])

function increment() {
  count.value++
}

function onSelectUser(user: User) {
  console.log(user.name)
}

const doubled = computed(() => count.value * 2)

function fetchUsers() {
  return fetch('/api/users')
}
</script>
