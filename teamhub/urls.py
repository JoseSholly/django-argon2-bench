from django.urls import path

from accounts import views_after, views_before

urlpatterns = [
    path("before/login/", views_before.LoginView.as_view()),
    path("before/refresh/", views_before.RefreshView.as_view()),
    path("after/login/", views_after.LoginView.as_view()),
    path("after/refresh/", views_after.RefreshView.as_view()),
]
