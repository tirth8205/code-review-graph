package com.example.app

import android.os.Bundle
import android.app.Activity

class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        initializeUI()
    }

    override fun onResume() {
        super.onResume()
        refreshData()
    }

    override fun onDestroy() {
        super.onDestroy()
        cleanup()
    }

    private fun initializeUI() {
        println("UI initialized")
    }

    private fun refreshData() {
        println("Data refreshed")
    }

    private fun cleanup() {
        println("Cleaned up")
    }
}
