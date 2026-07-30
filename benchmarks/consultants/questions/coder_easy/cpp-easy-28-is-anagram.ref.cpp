#include <iostream>
#include <string>
#include <algorithm>
static std::string rd() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    return s;
}
int main() {
    std::string a = rd(), b = rd();
    std::sort(a.begin(), a.end());
    std::sort(b.begin(), b.end());
    std::cout << (a == b ? "yes" : "no") << std::endl;
    return 0;
}
