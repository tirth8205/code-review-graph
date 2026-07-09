<template>
  <div class="container main-layout">
    <h1 class="title">{{ heading }}</h1>
    <span :class="{active: isActive}">Dynamic</span>
    <p class="description">Some text</p>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const heading = ref('Hello')
const isActive = ref(true)
</script>

<style>
.container { max-width: 1200px; }
.title { font-size: 2rem; }
.description { color: #666; }
</style>
