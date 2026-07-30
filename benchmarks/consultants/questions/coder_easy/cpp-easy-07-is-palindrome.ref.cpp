#include <iostream>
#include <string>
int main() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    std::string r(s.rbegin(), s.rend());
    std::cout << (s == r ? "yes" : "no") << std::endl;
    return 0;
}
